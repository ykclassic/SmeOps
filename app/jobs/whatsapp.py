import hashlib
import hmac
import json
import logging
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.core.config import get_settings
from app.core.whatsapp import decrypt_whatsapp_secret, encrypt_whatsapp_secret
from app.db.session import AsyncSessionFactory
from app.jobs.queue import ClaimedJob
from app.models import (
    WhatsAppConversation,
    WhatsAppConversationMessage,
    WhatsAppOutboundMessage,
    WhatsAppWebhookEvent,
)

logger = logging.getLogger(__name__)


def _customer_phone_hash(phone: str) -> str:
    key = get_settings().app_secret_key.get_secret_value().encode()
    return hmac.new(key, phone.encode(), hashlib.sha256).hexdigest()


def _provider_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.isdigit() or len(value) > 12:
        return None
    try:
        return datetime.fromtimestamp(int(value), tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


async def _process_inbound_message(session, *, tenant_id: UUID, payload: dict) -> None:
    message = payload.get("message")
    if not isinstance(message, dict):
        raise ValueError("Invalid inbound WhatsApp message payload")

    provider_message_id = message.get("id")
    sender = message.get("from")
    message_type = message.get("type")
    if (
        not isinstance(provider_message_id, str)
        or not 1 <= len(provider_message_id) <= 255
        or not isinstance(sender, str)
        or not sender.isdigit()
        or not 8 <= len(sender) <= 15
    ):
        raise ValueError("Inbound WhatsApp message identifiers are invalid")

    existing_message_result = await session.execute(
        select(WhatsAppConversationMessage).where(
            WhatsAppConversationMessage.tenant_id == tenant_id,
            WhatsAppConversationMessage.provider_message_id == provider_message_id,
        )
    )
    if existing_message_result.scalar_one_or_none() is not None:
        return

    safe_message_type = (
        message_type if isinstance(message_type, str) and 1 <= len(message_type) <= 40
        else "unknown"
    )
    body = None
    if safe_message_type == "text":
        text_payload = message.get("text")
        if isinstance(text_payload, dict) and isinstance(text_payload.get("body"), str):
            candidate = text_payload["body"]
            if len(candidate) <= 4096:
                body = candidate

    provider_timestamp = _provider_timestamp(message.get("timestamp"))
    received_at = provider_timestamp or datetime.now(UTC)
    phone_hash = _customer_phone_hash(sender)

    conversation_result = await session.execute(
        select(WhatsAppConversation)
        .where(
            WhatsAppConversation.tenant_id == tenant_id,
            WhatsAppConversation.customer_phone_hash == phone_hash,
        )
        .with_for_update()
    )
    conversation = conversation_result.scalar_one_or_none()
    if conversation is None:
        conversation = WhatsAppConversation(
            id=uuid4(),
            tenant_id=tenant_id,
            customer_phone_hash=phone_hash,
            customer_phone_encrypted=encrypt_whatsapp_secret(sender),
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
                select(WhatsAppConversation)
                .where(
                    WhatsAppConversation.tenant_id == tenant_id,
                    WhatsAppConversation.customer_phone_hash == phone_hash,
                )
                .with_for_update()
            )
            conversation = conversation_result.scalar_one_or_none()
            if conversation is None:
                raise
    else:
        conversation.last_inbound_at = max(conversation.last_inbound_at, received_at)

    conversation_message = WhatsAppConversationMessage(
        id=uuid4(),
        tenant_id=tenant_id,
        conversation_id=conversation.id,
        provider_message_id=provider_message_id,
        direction="inbound",
        message_type=safe_message_type,
        body_encrypted=encrypt_whatsapp_secret(body) if body is not None else None,
        provider_timestamp=provider_timestamp,
        delivery_status="received",
    )
    try:
        async with session.begin_nested():
            session.add(conversation_message)
            await session.flush()
    except IntegrityError:
        # Another worker may have won the provider-message uniqueness race.
        return


async def accept_inbound_whatsapp_job(job: ClaimedJob) -> None:
    try:
        event_id = UUID(job.payload["webhook_event_id"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("Invalid webhook event job payload") from exc

    async with AsyncSessionFactory() as session:
        result = await session.execute(
            select(WhatsAppWebhookEvent)
            .where(
                WhatsAppWebhookEvent.id == event_id,
                WhatsAppWebhookEvent.tenant_id == job.tenant_id,
            )
            .with_for_update()
        )
        event = result.scalar_one_or_none()
        if event is None:
            raise ValueError("Webhook event no longer exists")
        if event.status in {"accepted", "processed"}:
            return
        if event.status != "queued":
            raise ValueError("Webhook event is not in a processable state")

        payload = json.loads(decrypt_whatsapp_secret(event.payload_encrypted))

        if event.event_type == "message.status":
            provider_status = payload.get("status")
            if not isinstance(provider_status, dict):
                raise ValueError("Invalid WhatsApp delivery-status payload")

            provider_message_id = provider_status.get("id")
            provider_status_name = provider_status.get("status")
            if not isinstance(provider_message_id, str) or not isinstance(
                provider_status_name, str
            ):
                raise ValueError("WhatsApp delivery-status fields are missing")

            status_map = {
                "sent": "sent",
                "delivered": "delivered",
                "read": "read",
                "failed": "failed",
            }
            message_status = status_map.get(provider_status_name)
            if message_status is None:
                event.status = "processed"
                event.processed_at = datetime.now(UTC)
                await session.commit()
                return

            message_result = await session.execute(
                select(WhatsAppOutboundMessage)
                .where(
                    WhatsAppOutboundMessage.tenant_id == job.tenant_id,
                    WhatsAppOutboundMessage.provider_message_id == provider_message_id,
                )
                .with_for_update()
            )
            outbound_message = message_result.scalar_one_or_none()
            if outbound_message is None:
                raise RuntimeError("Outbound message not yet correlated to provider status")

            status_rank = {
                "queued": 0,
                "sending": 1,
                "unknown": 1,
                "sent": 2,
                "delivered": 3,
                "read": 4,
                "failed": 4,
            }
            current_rank = status_rank.get(outbound_message.status, -1)
            incoming_rank = status_rank[message_status]
            should_advance = (
                outbound_message.status not in {"read", "failed"}
                and incoming_rank >= current_rank
            )
            if should_advance:
                outbound_message.status = message_status
                if message_status == "failed":
                    outbound_message.last_error_code = "provider_delivery_failed"
            if message_status == "delivered":
                outbound_message.delivered_at = datetime.now(UTC)
            elif message_status == "read":
                outbound_message.read_at = datetime.now(UTC)

        elif event.event_type == "message.received":
            await _process_inbound_message(
                session,
                tenant_id=job.tenant_id,
                payload=payload,
            )

        event.status = "processed"
        event.processed_at = datetime.now(UTC)
        await session.commit()
