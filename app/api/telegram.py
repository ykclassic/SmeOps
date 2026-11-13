import hashlib
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
from app.core.telegram import (
    decrypt_telegram_secret,
    encrypt_telegram_secret,
    verify_telegram_webhook_secret,
)
from app.jobs.queue import enqueue_job
from app.models import (
    TelegramBookingReference,
    TelegramChatContext,
    TelegramConnection,
    TelegramOwnerLink,
    TelegramPlatformBot,
    TelegramWebhookEvent,
    Tenant,
    User,
)
from app.schemas.telegram import (
    TelegramConnectionResponse,
    TelegramOutboundMessageResponse,
    TelegramTextMessageRequest,
)
from app.services.telegram_outbound import (
    TelegramOutboundIdempotencyConflict,
    enqueue_telegram_text,
)
from app.services.telegram_webhook import (
    ensure_platform_webhook,
    platform_webhook_secret,
)

router = APIRouter(tags=["telegram integration"])
logger = logging.getLogger(__name__)
OwnerOnly = Annotated[User, Depends(require_roles("owner"))]
MAX_TELEGRAM_WEBHOOK_BYTES = 1_000_000


async def _lock_active_telegram_tenant(session, tenant_id) -> bool:
    """Serialize Telegram ingress with tenant lifecycle transitions."""
    with session.no_autoflush:
        result = await session.execute(
            select(Tenant).where(Tenant.id == tenant_id).with_for_update()
        )
    tenant = result.scalar_one_or_none()
    return tenant is not None and tenant.status == "active"



async def _ensure_platform_webhook(platform_bot: TelegramPlatformBot) -> None:
    try:
        await ensure_platform_webhook(platform_bot)
    except (RuntimeError, ValueError) as exc:
        logger.warning(
            "Shared Telegram webhook activation failed",
            extra={"error_type": type(exc).__name__},
        )
        raise HTTPException(
            status_code=503,
            detail="SmeOps Telegram connection could not be activated",
        ) from None


@router.post("/webhooks/telegram/platform", status_code=status.HTTP_200_OK)
async def receive_platform_telegram_webhook(
    request: Request,
    session: DbSession,
    secret_token: str | None = Header(
        default=None,
        alias="X-Telegram-Bot-Api-Secret-Token",
    ),
) -> dict[str, int | str]:
    platform_result = await session.execute(
        select(TelegramPlatformBot).where(
            TelegramPlatformBot.singleton_key == "platform",
            TelegramPlatformBot.status == "active",
        )
    )
    platform_bot = platform_result.scalar_one_or_none()
    if platform_bot is None:
        logger.warning(
            "Telegram platform webhook rejected",
            extra={"outcome": "rejected", "reason": "platform_bot_unavailable"},
        )
        raise HTTPException(status_code=404, detail="Platform Telegram bot is not configured")
    if not verify_telegram_webhook_secret(
        secret_token or "",
        platform_bot.webhook_secret_hash,
    ):
        logger.warning(
            "Telegram platform webhook rejected",
            extra={"outcome": "rejected", "reason": "invalid_secret"},
        )
        raise HTTPException(status_code=401, detail="Invalid Telegram webhook secret")

    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > MAX_TELEGRAM_WEBHOOK_BYTES:
                logger.warning(
                    "Telegram platform webhook rejected",
                    extra={"outcome": "rejected", "reason": "payload_too_large"},
                )
                raise HTTPException(status_code=413, detail="Webhook too large")
        except ValueError:
            logger.warning(
                "Telegram platform webhook rejected",
                extra={"outcome": "rejected", "reason": "invalid_content_length"},
            )
            raise HTTPException(status_code=400, detail="Invalid content length") from None

    body_buffer = bytearray()
    async for chunk in request.stream():
        body_buffer.extend(chunk)
        if len(body_buffer) > MAX_TELEGRAM_WEBHOOK_BYTES:
            logger.warning(
                "Telegram platform webhook rejected",
                extra={"outcome": "rejected", "reason": "payload_too_large"},
            )
            raise HTTPException(status_code=413, detail="Webhook too large")
    try:
        payload = json.loads(bytes(body_buffer))
    except (json.JSONDecodeError, UnicodeDecodeError):
        logger.warning(
            "Telegram platform webhook rejected",
            extra={"outcome": "rejected", "reason": "invalid_json"},
        )
        raise HTTPException(status_code=400, detail="Invalid JSON payload") from None
    if not isinstance(payload, dict):
        logger.warning(
            "Telegram platform webhook rejected",
            extra={"outcome": "rejected", "reason": "unsupported_payload_shape"},
        )
        raise HTTPException(status_code=400, detail="Unsupported Telegram update")
    update_id = payload.get("update_id")
    if isinstance(update_id, bool) or not isinstance(update_id, int) or update_id < 0:
        logger.warning(
            "Telegram platform webhook rejected",
            extra={"outcome": "rejected", "reason": "invalid_update_id"},
        )
        raise HTTPException(status_code=400, detail="Telegram update ID is invalid")

    update_context = {"update_id": update_id, "bot_id": platform_bot.bot_id}
    message = payload.get("message")
    if not isinstance(message, dict):
        logger.info(
            "Telegram platform webhook ignored",
            extra={**update_context, "outcome": "ignored", "reason": "unsupported_update_type"},
        )
        return {"status": "ignored", "queued_events": 0}
    chat = message.get("chat")
    sender = message.get("from")
    if (
        not isinstance(chat, dict)
        or chat.get("type") != "private"
        or not isinstance(sender, dict)
        or sender.get("is_bot") is True
    ):
        logger.info(
            "Telegram platform webhook ignored",
            extra={**update_context, "outcome": "ignored", "reason": "unsupported_chat_or_sender"},
        )
        return {"status": "ignored", "queued_events": 0}
    chat_id, sender_id = chat.get("id"), sender.get("id")
    if (
        isinstance(chat_id, bool)
        or not isinstance(chat_id, int)
        or isinstance(sender_id, bool)
        or not isinstance(sender_id, int)
        or chat_id <= 0
        or sender_id <= 0
        or chat_id != sender_id
    ):
        logger.info(
            "Telegram platform webhook ignored",
            extra={
                **update_context,
                "outcome": "ignored",
                "reason": "invalid_private_chat_identity",
            },
        )
        return {"status": "ignored", "queued_events": 0}

    canonical_payload = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    payload_hash = hashlib.sha256(canonical_payload.encode()).hexdigest()
    provider_update_id = str(update_id)
    duplicate_result = await session.execute(
        select(TelegramWebhookEvent).where(
            TelegramWebhookEvent.bot_id == platform_bot.bot_id,
            TelegramWebhookEvent.provider_update_id == provider_update_id,
        )
    )
    duplicate = duplicate_result.scalar_one_or_none()
    if duplicate is not None:
        logger.info(
            "Telegram platform webhook duplicate",
            extra={
                **update_context,
                "outcome": "duplicate",
                "event_id": str(duplicate.id),
                "payload_hash_matches": duplicate.payload_sha256 == payload_hash,
            },
        )
        return {"status": "duplicate", "queued_events": 0}

    tenant_id = None
    text_body = message.get("text")
    is_start_command = isinstance(text_body, str) and (
        text_body == "/start" or text_body.startswith("/start ")
    )
    token = ""
    if is_start_command:
        parts = text_body.split(maxsplit=1)
        token = parts[1].strip() if len(parts) == 2 else ""
    booking_chat_context = None
    booking_reference_resolved = False
    if token and len(token) <= 64:
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        owner_result = await session.execute(
            select(TelegramOwnerLink).where(
                TelegramOwnerLink.invitation_hash == token_hash,
                TelegramOwnerLink.expires_at > datetime.now(UTC),
                TelegramOwnerLink.linked_at.is_(None),
                TelegramOwnerLink.disabled_at.is_(None),
            )
        )
        owner_link = owner_result.scalar_one_or_none()
        if owner_link is not None:
            tenant_id = owner_link.tenant_id
        else:
            booking_result = await session.execute(
                select(TelegramBookingReference).where(
                    TelegramBookingReference.token_hash == token_hash,
                    TelegramBookingReference.revoked_at.is_(None),
                )
            )
            booking_ref = booking_result.scalar_one_or_none()
            if booking_ref is not None:
                tenant_id = booking_ref.tenant_id
                booking_reference_resolved = True
    if is_start_command and tenant_id is None:
        logger.info(
            "Telegram platform webhook ignored",
            extra={**update_context, "outcome": "ignored", "reason": "unresolved_start_link"},
        )
        return {"status": "ignored", "queued_events": 0}
    if tenant_id is None:
        context_result = await session.execute(
            select(TelegramChatContext).where(
                TelegramChatContext.platform_bot_id == platform_bot.id,
                TelegramChatContext.telegram_chat_id == str(chat_id),
                TelegramChatContext.telegram_user_id == str(sender_id),
            )
        )
        chat_context = context_result.scalar_one_or_none()
        tenant_id = chat_context.tenant_id if chat_context is not None else None
    if tenant_id is None:
        logger.info(
            "Telegram platform webhook ignored",
            extra={**update_context, "outcome": "ignored", "reason": "no_tenant_context"},
        )
        return {"status": "ignored", "queued_events": 0}

    # Lock the tenant before writing chat context, webhook events, or jobs. A
    # deactivation that wins this lock prevents the update from being queued.
    if not await _lock_active_telegram_tenant(session, tenant_id):
        logger.info(
            "Telegram platform webhook ignored",
            extra={**update_context, "outcome": "ignored", "reason": "tenant_inactive"},
        )
        return {"status": "ignored", "queued_events": 0}

    if booking_reference_resolved:
        # Acquire the tenant mutex before locking or changing chat context. This
        # keeps the lock order tenant -> chat context across Telegram ingress.
        context_result = await session.execute(
            select(TelegramChatContext).where(
                TelegramChatContext.platform_bot_id == platform_bot.id,
                TelegramChatContext.telegram_chat_id == str(chat_id),
            ).with_for_update()
        )
        booking_chat_context = context_result.scalar_one_or_none()

    if booking_reference_resolved and booking_chat_context is not None:
        booking_chat_context.tenant_id = tenant_id
        booking_chat_context.telegram_user_id = str(sender_id)
        booking_chat_context.selected_at = datetime.now(UTC)
        session.add(booking_chat_context)
    elif booking_reference_resolved and booking_chat_context is None:
        # A booking reference was resolved above; create the chat selection only
        # after the active-tenant lock has been acquired.
        booking_chat_context = TelegramChatContext(
            platform_bot_id=platform_bot.id,
            tenant_id=tenant_id,
            telegram_user_id=str(sender_id),
            telegram_chat_id=str(chat_id),
        )
        session.add(booking_chat_context)

    event = TelegramWebhookEvent(
        id=uuid4(),
        tenant_id=tenant_id,
        bot_id=platform_bot.bot_id,
        provider_update_id=provider_update_id,
        event_type="message.received",
        payload_encrypted=encrypt_telegram_secret(canonical_payload),
        payload_sha256=payload_hash,
        status="queued",
    )
    try:
        async with session.begin_nested():
            session.add(event)
            await session.flush()
    except IntegrityError:
        # A concurrent request may have inserted the same update after the first
        # duplicate lookup. Only acknowledge it as a duplicate after verifying
        # the durable row; otherwise propagate the database failure.
        duplicate_result = await session.execute(
            select(TelegramWebhookEvent).where(
                TelegramWebhookEvent.bot_id == platform_bot.bot_id,
                TelegramWebhookEvent.provider_update_id == provider_update_id,
            )
        )
        duplicate = duplicate_result.scalar_one_or_none()
        if duplicate is None or duplicate.payload_sha256 != payload_hash:
            raise
        logger.info(
            "Telegram platform webhook duplicate",
            extra={
                **update_context,
                "outcome": "duplicate",
                "event_id": str(duplicate.id),
                "payload_hash_matches": True,
                "race_recovered": True,
            },
        )
        return {"status": "duplicate", "queued_events": 0}

    try:
        job = await enqueue_job(
            session,
            tenant_id=tenant_id,
            job_type="telegram.accept_inbound",
            payload={"webhook_event_id": str(event.id)},
            idempotency_key=f"tg:platform:{platform_bot.bot_id}:{provider_update_id}",
            max_attempts=3,
        )
        await session.commit()
    except Exception as exc:
        # Do not return a successful Telegram acknowledgement unless the event
        # and its job have committed in the same database transaction.
        logger.error(
            "Telegram platform webhook persistence failed",
            extra={
                **update_context,
                "outcome": "rejected",
                "reason": "persistence_failed",
                "error_type": type(exc).__name__,
            },
        )
        raise

    logger.info(
        "Telegram platform webhook accepted",
        extra={
            **update_context,
            "outcome": "accepted",
            "event_id": str(event.id),
            "tenant_id": str(tenant_id),
        },
    )
    logger.info(
        "Telegram platform webhook job enqueued",
        extra={
            **update_context,
            "outcome": "enqueued",
            "event_id": str(event.id),
            "job_id": str(job.id) if getattr(job, "id", None) is not None else None,
            "job_type": "telegram.accept_inbound",
        },
    )
    return {"status": "accepted", "queued_events": 1}

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

    # Legacy per-tenant webhook: verify lifecycle state under the same tenant
    # mutex used by REST mutations before deduplication or event persistence.
    if not await _lock_active_telegram_tenant(session, connection.tenant_id):
        logger.info(
            "Telegram tenant webhook ignored",
            extra={"bot_id": bot_id, "update_id": update_id, "reason": "tenant_inactive"},
        )
        return {"status": "ignored", "queued_events": 0}

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
        bot_id=connection.bot_id,
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
    return platform_webhook_secret(bot_id)


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
    await session.commit()
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
    await _ensure_platform_webhook(platform_bot)

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

    token = f"owner_{secrets.token_urlsafe(24)}"
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
    now = datetime.now(UTC)
    linked = bool(
        link is not None
        and link.linked_at is not None
        and link.disabled_at is None
        and link.chat_id is not None
    )
    pending = bool(
        link is not None
        and link.linked_at is None
        and link.disabled_at is None
        and link.expires_at > now
    )
    return {
        "linked": linked,
        "status": "active" if linked else "pending" if pending else "disconnected",
        "linked_at": link.linked_at.isoformat() if linked and link else None,
        "expires_at": link.expires_at.isoformat() if pending and link else None,
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
    await _ensure_platform_webhook(platform_bot)

    result = await session.execute(
        select(TelegramBookingReference)
        .where(TelegramBookingReference.tenant_id == user.tenant_id)
        .with_for_update()
    )
    reference = result.scalar_one_or_none()
    token = f"book_{secrets.token_urlsafe(24)}"
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
