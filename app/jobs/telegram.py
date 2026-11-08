import hashlib
import json
import logging
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.core.telegram import decrypt_telegram_secret, encrypt_telegram_secret
from app.db.session import AsyncSessionFactory
from app.jobs.queue import ClaimedJob, enqueue_job
from app.models import (
    AuditEvent,
    TelegramConversation,
    TelegramConversationMessage,
    TelegramOwnerLink,
    TelegramWebhookEvent,
)

logger = logging.getLogger(__name__)


def _message_timestamp(value: object) -> datetime:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return datetime.now(UTC)
    try:
        return datetime.fromtimestamp(value, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return datetime.now(UTC)


async def _persist_telegram_message(
    session, *, tenant_id: UUID, payload: dict
) -> tuple[UUID, UUID] | None:
    message = payload.get("message")
    if not isinstance(message, dict):
        return None

    chat = message.get("chat")
    sender = message.get("from")
    if not isinstance(chat, dict) or not isinstance(sender, dict):
        return None
    if chat.get("type") != "private" or sender.get("is_bot") is True:
        return None
    chat_id = chat.get("id")
    user_id = sender.get("id")
    message_id = message.get("message_id")
    if any(isinstance(value, bool) or not isinstance(value, int) for value in
           (chat_id, user_id, message_id)):
        return
    if chat_id == 0 or user_id <= 0 or message_id <= 0:
        return None

    text_body = message.get("text")
    contact = message.get("contact")
    if (
        not isinstance(text_body, str)
        and isinstance(contact, dict)
        and contact.get("user_id") == user_id
        and isinstance(contact.get("phone_number"), str)
    ):
        text_body = "contact:" + contact["phone_number"]
    if isinstance(text_body, str) and len(text_body) > 4096:
        text_body = None
    if not isinstance(text_body, str):
        text_body = None

    conversation_result = await session.execute(
        select(TelegramConversation)
        .where(
            TelegramConversation.tenant_id == tenant_id,
            TelegramConversation.telegram_chat_id == str(chat_id),
        )
        .with_for_update()
    )
    conversation = conversation_result.scalar_one_or_none()
    received_at = _message_timestamp(message.get("date"))
    if conversation is None:
        first_name = sender.get("first_name")
        last_name = sender.get("last_name")
        name_parts = [
            value.strip()
            for value in (first_name, last_name)
            if isinstance(value, str) and value.strip()
        ]
        conversation = TelegramConversation(
            id=uuid4(),
            tenant_id=tenant_id,
            telegram_user_id=str(user_id),
            telegram_chat_id=str(chat_id),
            customer_name=" ".join(name_parts)[:160] or None,
            status="active",
            context={},
            last_inbound_at=received_at,
        )
        try:
            async with session.begin_nested():
                session.add(conversation)
                await session.flush()
        except IntegrityError:
            conversation_result = await session.execute(
                select(TelegramConversation)
                .where(
                    TelegramConversation.tenant_id == tenant_id,
                    TelegramConversation.telegram_chat_id == str(chat_id),
                )
                .with_for_update()
            )
            conversation = conversation_result.scalar_one_or_none()
            if conversation is None:
                raise
    else:
        conversation.last_inbound_at = max(conversation.last_inbound_at, received_at)
        conversation.telegram_user_id = str(user_id)

    provider_message_id = f"{chat_id}:{message_id}"
    duplicate_result = await session.execute(
        select(TelegramConversationMessage).where(
            TelegramConversationMessage.tenant_id == tenant_id,
            TelegramConversationMessage.provider_message_id == provider_message_id,
        )
    )
    if duplicate_result.scalar_one_or_none() is not None:
        return None

    record = TelegramConversationMessage(
        id=uuid4(),
        tenant_id=tenant_id,
        conversation_id=conversation.id,
        provider_message_id=provider_message_id,
        direction="inbound",
        body_encrypted=encrypt_telegram_secret(text_body) if text_body else None,
        delivery_status="received",
    )
    try:
        async with session.begin_nested():
            session.add(record)
            await session.flush()
    except IntegrityError:
        logger.info("Duplicate Telegram message ignored")
        return None
    return conversation.id, record.id


async def _consume_owner_link(session, *, tenant_id: UUID, payload: dict) -> bool:
    """Consume a short-lived /start invitation before treating it as a customer chat."""
    message = payload.get("message")
    if not isinstance(message, dict):
        return False
    text_body = message.get("text")
    if not isinstance(text_body, str) or not (text_body == "/start" or text_body.startswith("/start ")):
        return False

    parts = text_body.split(maxsplit=1)
    token = parts[1].strip() if len(parts) == 2 else ""
    chat = message.get("chat")
    sender = message.get("from")
    if (
        not token
        or len(token) > 64
        or not isinstance(chat, dict)
        or chat.get("type") != "private"
        or not isinstance(sender, dict)
        or sender.get("is_bot") is True
    ):
        return True

    chat_id = chat.get("id")
    sender_id = sender.get("id")
    if (
        isinstance(chat_id, bool)
        or not isinstance(chat_id, int)
        or isinstance(sender_id, bool)
        or not isinstance(sender_id, int)
        or sender_id <= 0
    ):
        return True

    token_hash = hashlib.sha256(token.encode()).hexdigest()
    result = await session.execute(
        select(TelegramOwnerLink)
        .where(
            TelegramOwnerLink.invitation_hash == token_hash,
            TelegramOwnerLink.tenant_id == tenant_id,
            TelegramOwnerLink.expires_at > datetime.now(UTC),
            TelegramOwnerLink.linked_at.is_(None),
            TelegramOwnerLink.disabled_at.is_(None),
        )
        .with_for_update()
    )
    link = result.scalar_one_or_none()
    if link is None:
        logger.info("Telegram owner-link invitation rejected or expired")
        return True

    link.chat_id = str(chat_id)
    link.linked_at = datetime.now(UTC)
    session.add(
        AuditEvent(
            tenant_id=tenant_id,
            actor_user_id=link.owner_user_id,
            event_type="telegram.owner_link.connected",
            subject_type="telegram_owner_link",
            subject_id=link.id,
            payload={},
        )
    )
    return True


async def accept_inbound_telegram_job(job: ClaimedJob) -> None:
    try:
        event_id = UUID(job.payload["webhook_event_id"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("Invalid Telegram webhook job payload") from exc

    async with AsyncSessionFactory() as session:
        result = await session.execute(
            select(TelegramWebhookEvent)
            .where(
                TelegramWebhookEvent.id == event_id,
                TelegramWebhookEvent.tenant_id == job.tenant_id,
            )
            .with_for_update()
        )
        event = result.scalar_one_or_none()
        if event is None:
            raise ValueError("Telegram webhook event no longer exists")
        if event.status == "processed":
            return
        if event.status != "queued":
            raise ValueError("Telegram webhook event is not processable")

        payload = json.loads(decrypt_telegram_secret(event.payload_encrypted))
        owner_link_message = await _consume_owner_link(
            session,
            tenant_id=job.tenant_id,
            payload=payload,
        )
        persisted = None if owner_link_message else await _persist_telegram_message(
            session,
            tenant_id=job.tenant_id,
            payload=payload,
        )
        if persisted is not None:
            conversation_id, message_id = persisted
            await enqueue_job(
                session,
                tenant_id=job.tenant_id,
                job_type="telegram.process_conversation",
                payload={
                    "conversation_id": str(conversation_id),
                    "message_id": str(message_id),
                },
                idempotency_key=f"tg:conversation:{message_id}",
                max_attempts=3,
            )
        event.status = "processed"
        event.processed_at = datetime.now(UTC)
        await session.commit()
