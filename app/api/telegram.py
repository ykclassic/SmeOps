import hashlib
import hmac
import json
import logging
import secrets
from datetime import UTC, datetime, timedelta
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.api.dependencies import DbSession, require_roles
from app.core.config import get_settings
from app.core.telegram import (
    decrypt_telegram_secret,
    encrypt_telegram_secret,
    hash_telegram_webhook_secret,
    verify_telegram_webhook_secret,
)
from app.integrations.telegram_bot import (
    TelegramAPIError,
    TelegramDeliveryUnknown,
    get_bot_identity,
    register_webhook,
)
from app.jobs.queue import enqueue_job
from app.models import (
    AuditEvent,
    TelegramConnection,
    TelegramOwnerLink,
    TelegramPlatformBot,
    TelegramBookingReference,
    TelegramWebhookEvent,
    User,
)
from app.schemas.telegram import (
    TelegramConnectionProvision,
    TelegramConnectionResponse,
    TelegramOutboundMessageResponse,
    TelegramTextMessageRequest,
)
from app.services.telegram_outbound import (
    TelegramOutboundIdempotencyConflict,
    enqueue_telegram_text,
)

router = APIRouter(tags=["telegram integration"])
logger = logging.getLogger(__name__)
OwnerOnly = Annotated[User, Depends(require_roles("owner"))]
MAX_TELEGRAM_WEBHOOK_BYTES = 1_000_000


@router.post("/webhooks/telegram/{bot_id}", status_code=status.HTTP_200_OK)
async def receive_telegram_webhook(
    bot_id: str,
    request: Request,
    session: DbSession,
    secret_token: str | None = Header(
        default=None,
        alias="X-Telegram-Bot-Api-Secret-Token",
    ),
) -> dict[str, int | str]:
    if not bot_id.isdigit() or not 5 <= len(bot_id) <= 80:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Telegram connection not found",
        )

    connection_result = await session.execute(
        select(TelegramConnection).where(
            TelegramConnection.bot_id == bot_id,
            TelegramConnection.status == "active",
        )
    )
    connection = connection_result.scalar_one_or_none()
    if connection is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Telegram connection not found",
        )
    if not verify_telegram_webhook_secret(
        secret_token or "",
        connection.webhook_secret_hash,
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid Telegram webhook secret",
        )

    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > MAX_TELEGRAM_WEBHOOK_BYTES:
                raise HTTPException(
                    status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    detail="Webhook too large",
                )
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid content length",
            ) from None

    body_buffer = bytearray()
    async for chunk in request.stream():
        body_buffer.extend(chunk)
        if len(body_buffer) > MAX_TELEGRAM_WEBHOOK_BYTES:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail="Webhook too large",
            )
    body = bytes(body_buffer)
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid JSON payload",
        ) from None
    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Unsupported Telegram update",
        )

    update_id = payload.get("update_id")
    if isinstance(update_id, bool) or not isinstance(update_id, int) or update_id < 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Telegram update ID is invalid",
        )

    canonical_payload = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    payload_hash = hashlib.sha256(canonical_payload.encode()).hexdigest()
    provider_update_id = str(update_id)
    existing_result = await session.execute(
        select(TelegramWebhookEvent).where(
            TelegramWebhookEvent.tenant_id == connection.tenant_id,
            TelegramWebhookEvent.provider_update_id == provider_update_id,
        )
    )
    existing = existing_result.scalar_one_or_none()
    if existing is not None:
        if existing.payload_sha256 != payload_hash:
            logger.warning("Telegram duplicate update payload hash mismatch")
        return {"status": "accepted", "queued_events": 0}

    event = TelegramWebhookEvent(
        id=uuid4(),
        tenant_id=connection.tenant_id,
        provider_update_id=provider_update_id,
        event_type="message.received" if isinstance(payload.get("message"), dict) else "update",
        payload_encrypted=encrypt_telegram_secret(canonical_payload),
        payload_sha256=payload_hash,
        status="queued",
    )
    try:
        async with session.begin_nested():
            session.add(event)
            await session.flush()
    except IntegrityError:
        duplicate_result = await session.execute(
            select(TelegramWebhookEvent).where(
                TelegramWebhookEvent.tenant_id == connection.tenant_id,
                TelegramWebhookEvent.provider_update_id == provider_update_id,
            )
        )
        duplicate = duplicate_result.scalar_one_or_none()
        if duplicate is not None and duplicate.payload_sha256 == payload_hash:
            return {"status": "accepted", "queued_events": 0}
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Telegram update conflicts with an existing event",
        ) from None

    await enqueue_job(
        session,
        tenant_id=connection.tenant_id,
        job_type="telegram.accept_inbound",
        payload={"webhook_event_id": str(event.id)},
        idempotency_key=f"tg:update:{provider_update_id}",
        max_attempts=3,
    )
    await session.commit()
    return {"status": "accepted", "queued_events": 1}



def _telegram_webhook_secret(bot_id: str) -> str:
    key = get_settings().app_secret_key.get_secret_value().encode()
    return hmac.new(
        key,
        f"smeops:telegram:webhook:{bot_id}".encode(),
        hashlib.sha256,
    ).hexdigest()


async def _register_connection_webhook(connection: TelegramConnection) -> None:
    settings = get_settings()
    if settings.public_api_base_url is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="PUBLIC_API_BASE_URL must be configured before connecting Telegram",
        )
    token = decrypt_telegram_secret(connection.bot_token_encrypted)
    await register_webhook(
        bot_token=token,
        webhook_url=f"{settings.public_api_base_url}/webhooks/telegram/{connection.bot_id}",
        secret_token=_telegram_webhook_secret(connection.bot_id),
    )


@router.post(
    "/telegram/connection",
    response_model=TelegramConnectionResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def provision_telegram_connection(
    payload: TelegramConnectionProvision,
    session: DbSession,
    user: OwnerOnly,
) -> TelegramConnectionResponse:
    settings = get_settings()
    if settings.whatsapp_token_encryption_key is None:
        raise HTTPException(
            status_code=503,
            detail="Channel token encryption is not configured",
        )
    if settings.public_api_base_url is None:
        raise HTTPException(status_code=503, detail="PUBLIC_API_BASE_URL is not configured")

    token = payload.bot_token.get_secret_value()
    try:
        identity = await get_bot_identity(bot_token=token)
    except (TelegramAPIError, TelegramDeliveryUnknown, ValueError):
        raise HTTPException(
            status_code=502,
            detail="Telegram bot credentials could not be verified",
        ) from None

    result = await session.execute(
        select(TelegramConnection)
        .where(TelegramConnection.tenant_id == user.tenant_id)
        .with_for_update()
    )
    connection = result.scalar_one_or_none()
    if connection is not None and connection.bot_id != identity.bot_id:
        raise HTTPException(status_code=409, detail="Disconnect the existing Telegram bot first")

    if connection is None:
        connection = TelegramConnection(
            tenant_id=user.tenant_id,
            bot_id=identity.bot_id,
            bot_username=identity.username,
            bot_token_encrypted=encrypt_telegram_secret(token),
            webhook_secret_hash=hash_telegram_webhook_secret(_telegram_webhook_secret(identity.bot_id)),
            status="pending",
        )
    else:
        connection.bot_username = identity.username
        connection.bot_token_encrypted = encrypt_telegram_secret(token)
        connection.webhook_secret_hash = hash_telegram_webhook_secret(
            _telegram_webhook_secret(identity.bot_id)
        )
        connection.status = "pending"

    session.add(connection)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(
            status_code=409,
            detail="This Telegram bot is already connected to another workspace",
        ) from None

    try:
        await _register_connection_webhook(connection)
    except (TelegramAPIError, TelegramDeliveryUnknown, ValueError, HTTPException):
        logger.warning(
            "Telegram webhook registration remains pending",
            extra={"bot_id": identity.bot_id},
        )
        return TelegramConnectionResponse(
            bot_id=connection.bot_id,
            bot_username=connection.bot_username,
            status="pending",
        )

    connection.status = "active"
    session.add(AuditEvent(
        tenant_id=user.tenant_id,
        actor_user_id=user.id,
        event_type="telegram.connection.provisioned",
        subject_type="telegram_connection",
        subject_id=connection.id,
        payload={"bot_id": connection.bot_id},
    ))
    await session.commit()
    return TelegramConnectionResponse(
        bot_id=connection.bot_id,
        bot_username=connection.bot_username,
        status=connection.status,
    )


@router.post(
    "/telegram/connection/complete",
    response_model=TelegramConnectionResponse,
)
async def complete_telegram_connection(
    session: DbSession,
    user: OwnerOnly,
) -> TelegramConnectionResponse:
    result = await session.execute(
        select(TelegramConnection)
        .where(
            TelegramConnection.tenant_id == user.tenant_id,
            TelegramConnection.status == "pending",
        )
        .with_for_update()
    )
    connection = result.scalar_one_or_none()
    if connection is None:
        raise HTTPException(status_code=409, detail="There is no pending Telegram connection")
    try:
        await _register_connection_webhook(connection)
    except (TelegramAPIError, TelegramDeliveryUnknown, ValueError, HTTPException):
        await session.rollback()
        raise HTTPException(
            status_code=502,
            detail="Telegram webhook registration is still pending",
        ) from None
    connection.status = "active"
    await session.commit()
    return TelegramConnectionResponse(
        bot_id=connection.bot_id,
        bot_username=connection.bot_username,
        status=connection.status,
    )


@router.get("/telegram/connection", response_model=TelegramConnectionResponse | None)
async def get_telegram_connection(
    session: DbSession,
    user: OwnerOnly,
) -> TelegramConnectionResponse | None:
    result = await session.execute(
        select(TelegramConnection).where(TelegramConnection.tenant_id == user.tenant_id)
    )
    connection = result.scalar_one_or_none()
    if connection is None:
        return None
    return TelegramConnectionResponse(
        bot_id=connection.bot_id,
        bot_username=connection.bot_username,
        status=connection.status,
    )


@router.post(
    "/telegram/messages",
    response_model=TelegramOutboundMessageResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def queue_telegram_text_message(
    payload: TelegramTextMessageRequest,
    session: DbSession,
    user: OwnerOnly,
) -> TelegramOutboundMessageResponse:
    try:
        message = await enqueue_telegram_text(
            session,
            tenant_id=user.tenant_id,
            conversation_id=payload.conversation_id,
            body=payload.body,
            idempotency_key=str(payload.idempotency_key),
            actor_user_id=user.id,
        )
    except TelegramOutboundIdempotencyConflict:
        raise HTTPException(
            status_code=409,
            detail="Idempotency key was used for different content",
        ) from None
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return TelegramOutboundMessageResponse(id=message.id, status=message.status)



@router.post("/telegram/owner-link")
async def create_telegram_owner_link(
    session: DbSession,
    user: OwnerOnly,
) -> dict[str, str | bool]:
    platform_result = await session.execute(
        select(TelegramPlatformBot).where(
            TelegramPlatformBot.singleton_key == "platform",
            TelegramPlatformBot.status == "active",
        )
    )
    platform_bot = platform_result.scalar_one_or_none()
    if platform_bot is None:
        raise HTTPException(status_code=503, detail="SmeOps Telegram is temporarily unavailable")

    result = await session.execute(
        select(TelegramOwnerLink)
        .where(
            TelegramOwnerLink.tenant_id == user.tenant_id,
            TelegramOwnerLink.owner_user_id == user.id,
        )
        .with_for_update()
    )
    link = result.scalar_one_or_none()
    if (
        link is not None
        and link.linked_at is not None
        and link.disabled_at is None
        and link.chat_id is not None
    ):
        return {"linked": True, "status": "active"}

    token = secrets.token_urlsafe(24)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    expires_at = datetime.now(UTC) + timedelta(minutes=15)
    if link is None:
        link = TelegramOwnerLink(
            tenant_id=user.tenant_id,
            owner_user_id=user.id,
            invitation_hash=token_hash,
            expires_at=expires_at,
        )
    else:
        link.invitation_hash = token_hash
        link.expires_at = expires_at
        link.chat_id = None
        link.linked_at = None
        link.disabled_at = None
    session.add(link)
    await session.commit()
    return {
        "linked": False,
        "status": "pending",
        "expires_at": expires_at.isoformat(),
        "deep_link": f"https://t.me/{platform_bot.bot_username}?start={token}",
    }


@router.get("/telegram/owner-link")
async def get_telegram_owner_link_status(
    session: DbSession,
    user: OwnerOnly,
) -> dict[str, str | bool | None]:
    result = await session.execute(
        select(TelegramOwnerLink).where(
            TelegramOwnerLink.tenant_id == user.tenant_id,
            TelegramOwnerLink.owner_user_id == user.id,
        )
    )
    link = result.scalar_one_or_none()
    linked = bool(
        link is not None
        and link.linked_at is not None
        and link.disabled_at is None
        and link.chat_id is not None
    )
    return {
        "linked": linked,
        "status": "active" if linked else "disconnected",
        "linked_at": link.linked_at.isoformat() if linked and link else None,
    }


@router.delete("/telegram/owner-link")
async def disconnect_telegram_owner_link(
    session: DbSession,
    user: OwnerOnly,
) -> dict[str, str]:
    result = await session.execute(
        select(TelegramOwnerLink)
        .where(
            TelegramOwnerLink.tenant_id == user.tenant_id,
            TelegramOwnerLink.owner_user_id == user.id,
        )
        .with_for_update()
    )
    link = result.scalar_one_or_none()
    if link is not None:
        link.disabled_at = datetime.now(UTC)
        link.invitation_hash = hashlib.sha256(secrets.token_bytes(32)).hexdigest()
        link.expires_at = datetime.now(UTC)
        await session.commit()
    return {"status": "disconnected"}


@router.get("/telegram/booking-link")
async def get_telegram_booking_link(
    session: DbSession,
    user: OwnerOnly,
) -> dict[str, str | bool | None]:
    platform_result = await session.execute(
        select(TelegramPlatformBot).where(
            TelegramPlatformBot.singleton_key == "platform",
            TelegramPlatformBot.status == "active",
        )
    )
    platform_bot = platform_result.scalar_one_or_none()
    if platform_bot is None:
        return {"connected": False, "booking_link": None}
    result = await session.execute(
        select(TelegramBookingReference).where(
            TelegramBookingReference.tenant_id == user.tenant_id,
            TelegramBookingReference.revoked_at.is_(None),
        )
    )
    reference = result.scalar_one_or_none()
    if reference is None:
        return {"connected": True, "booking_link": None}
    token = decrypt_telegram_secret(reference.token_encrypted)
    return {
        "connected": True,
        "booking_link": f"https://t.me/{platform_bot.bot_username}?start={token}",
        "status": "active",
    }


@router.post("/telegram/booking-link")
async def regenerate_telegram_booking_link(
    session: DbSession,
    user: OwnerOnly,
) -> dict[str, str | bool]:
    platform_result = await session.execute(
        select(TelegramPlatformBot).where(
            TelegramPlatformBot.singleton_key == "platform",
            TelegramPlatformBot.status == "active",
        )
    )
    platform_bot = platform_result.scalar_one_or_none()
    if platform_bot is None:
        raise HTTPException(status_code=503, detail="SmeOps Telegram is temporarily unavailable")

    result = await session.execute(
        select(TelegramBookingReference)
        .where(TelegramBookingReference.tenant_id == user.tenant_id)
        .with_for_update()
    )
    reference = result.scalar_one_or_none()
    token = secrets.token_urlsafe(24)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    if reference is None:
        reference = TelegramBookingReference(
            tenant_id=user.tenant_id,
            token_hash=token_hash,
            token_encrypted=encrypt_telegram_secret(token),
        )
    else:
        reference.token_hash = token_hash
        reference.token_encrypted = encrypt_telegram_secret(token)
        reference.revoked_at = None
    session.add(reference)
    await session.commit()
    return {
        "connected": True,
        "booking_link": f"https://t.me/{platform_bot.bot_username}?start={token}",
        "status": "active",
    }
