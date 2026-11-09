import hashlib
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.telegram import decrypt_telegram_secret, encrypt_telegram_secret
from app.jobs.queue import enqueue_job
from app.models import (
    AuditEvent,
    TelegramConversation,
    TelegramConversationMessage,
    TelegramOutboundMessage,
    TelegramPlatformBot,
)


class TelegramOutboundIdempotencyConflict(ValueError):
    """An idempotency key was reused for different message content."""


def _matches_request(
    message: TelegramOutboundMessage,
    *,
    conversation_id: UUID,
    body: str,
) -> bool:
    return (
        message.conversation_id == conversation_id
        and decrypt_telegram_secret(message.body_encrypted) == body
    )


async def enqueue_telegram_text(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    conversation_id: UUID,
    body: str,
    idempotency_key: str,
    actor_user_id: UUID | None = None,
) -> TelegramOutboundMessage:
    normalized_body = body.strip()
    if not normalized_body or len(normalized_body) > 4096:
        raise ValueError("Message body must contain 1 to 4096 characters")
    if not 1 <= len(idempotency_key) <= 160:
        raise ValueError("Invalid idempotency key")

    existing_result = await session.execute(
        select(TelegramOutboundMessage).where(
            TelegramOutboundMessage.tenant_id == tenant_id,
            TelegramOutboundMessage.idempotency_key == idempotency_key,
        )
    )
    existing = existing_result.scalar_one_or_none()
    if existing is not None:
        if not _matches_request(
            existing,
            conversation_id=conversation_id,
            body=normalized_body,
        ):
            raise TelegramOutboundIdempotencyConflict(
                "Idempotency key was reused with different message content"
            )
        return existing

    conversation_result = await session.execute(
        select(TelegramConversation).where(
            TelegramConversation.id == conversation_id,
            TelegramConversation.tenant_id == tenant_id,
            TelegramConversation.status != "closed",
        )
    )
    conversation = conversation_result.scalar_one_or_none()
    if conversation is None:
        raise ValueError("Telegram conversation not found or closed")

    platform_result = await session.execute(
        select(TelegramPlatformBot).where(
            TelegramPlatformBot.singleton_key == "platform",
            TelegramPlatformBot.status == "active",
        )
    )
    if platform_result.scalar_one_or_none() is None:
        raise ValueError("SmeOps Telegram platform bot is not active")

    message = TelegramOutboundMessage(
        id=uuid4(),
        tenant_id=tenant_id,
        conversation_id=conversation.id,
        recipient_chat_id=conversation.telegram_chat_id,
        body_encrypted=encrypt_telegram_secret(normalized_body),
        idempotency_key=idempotency_key,
        status="queued",
    )
    try:
        async with session.begin_nested():
            session.add(message)
            await session.flush()
    except IntegrityError:
        duplicate_result = await session.execute(
            select(TelegramOutboundMessage).where(
                TelegramOutboundMessage.tenant_id == tenant_id,
                TelegramOutboundMessage.idempotency_key == idempotency_key,
            )
        )
        duplicate = duplicate_result.scalar_one_or_none()
        if duplicate is None or not _matches_request(
            duplicate,
            conversation_id=conversation_id,
            body=normalized_body,
        ):
            raise TelegramOutboundIdempotencyConflict(
                "Idempotency key was concurrently reused with different content"
            ) from None
        return duplicate

    session.add(
        TelegramConversationMessage(
            tenant_id=tenant_id,
            conversation_id=conversation.id,
            provider_message_id=f"outbound:{message.id}",
            direction="outbound",
            body_encrypted=encrypt_telegram_secret(normalized_body),
            delivery_status="queued",
        )
    )
    session.add(
        AuditEvent(
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            event_type="telegram.message.queued",
            subject_type="telegram_outbound_message",
            subject_id=message.id,
            payload={
                "idempotency_key_sha256": hashlib.sha256(
                    idempotency_key.encode()
                ).hexdigest()
            },
        )
    )
    await enqueue_job(
        session,
        tenant_id=tenant_id,
        job_type="telegram.send_text",
        payload={"message_id": str(message.id)},
        idempotency_key=(
            "tg:outbound:"
            + hashlib.sha256(idempotency_key.encode()).hexdigest()
        ),
        max_attempts=2,
    )
    # The caller owns the transaction. Keeping this service commit-free allows
    # lifecycle mutations, audit records, outbound rows, and jobs to commit atomically.
    return message
