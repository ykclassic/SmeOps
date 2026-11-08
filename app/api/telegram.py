import hashlib
import json
import logging
from uuid import uuid4

from fastapi import APIRouter, Header, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.api.dependencies import DbSession
from app.core.telegram import (
    encrypt_telegram_secret,
    verify_telegram_webhook_secret,
)
from app.jobs.queue import enqueue_job
from app.models import TelegramConnection, TelegramWebhookEvent

router = APIRouter(tags=["telegram integration"])
logger = logging.getLogger(__name__)
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

    body = await request.body()
    if len(body) > MAX_TELEGRAM_WEBHOOK_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="Webhook too large",
        )
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
