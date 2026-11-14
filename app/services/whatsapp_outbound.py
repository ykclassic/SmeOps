import hashlib
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.whatsapp import (
    decrypt_whatsapp_secret,
    encrypt_whatsapp_secret,
    hash_whatsapp_contact,
)
from app.jobs.queue import enqueue_job
from app.models import (
    AuditEvent,
    Tenant,
    WhatsAppConnection,
    WhatsAppConversation,
    WhatsAppConversationMessage,
    WhatsAppOutboundMessage,
)

CUSTOMER_SERVICE_WINDOW = timedelta(hours=24)


class OutboundIdempotencyConflict(ValueError):
    """An outbound idempotency key was reused for different content."""


class OutboundPolicyViolation(ValueError):
    """The requested message is not permitted by WhatsApp contact policy."""

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


def _matches_request(
    message: WhatsAppOutboundMessage,
    *,
    recipient: str,
    body: str,
    deliver_after_deactivation: bool,
) -> bool:
    return (
        decrypt_whatsapp_secret(message.recipient_encrypted) == recipient
        and decrypt_whatsapp_secret(message.body_encrypted) == body
        and bool(message.deliver_after_deactivation) == deliver_after_deactivation
    )


async def assert_outbound_policy(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    recipient: str,
    now: datetime | None = None,
) -> WhatsAppConversation:
    current_time = now or datetime.now(UTC)
    phone_hash = hash_whatsapp_contact(recipient)
    result = await session.execute(
        select(WhatsAppConversation).where(
            WhatsAppConversation.tenant_id == tenant_id,
            WhatsAppConversation.customer_phone_hash == phone_hash,
        )
    )
    conversation = result.scalar_one_or_none()
    if conversation is None:
        raise OutboundPolicyViolation(
            "customer_service_window_closed",
            "A customer-initiated WhatsApp conversation is required before sending text.",
        )
    if conversation.is_opted_out:
        raise OutboundPolicyViolation(
            "customer_opted_out",
            "The customer has opted out of WhatsApp messages.",
        )
    if conversation.last_inbound_at <= current_time - CUSTOMER_SERVICE_WINDOW:
        raise OutboundPolicyViolation(
            "customer_service_window_closed",
            "The 24-hour WhatsApp customer-service window has expired.",
        )
    return conversation


async def enqueue_whatsapp_text(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    recipient: str,
    body: str,
    idempotency_key: str,
    actor_user_id: UUID | None = None,
    deliver_after_deactivation: bool = False,
) -> WhatsAppOutboundMessage:
    normalized_recipient = recipient.strip().lstrip("+")
    if not normalized_recipient.isdigit() or not 8 <= len(normalized_recipient) <= 15:
        raise ValueError("recipient must be an international phone number")
    normalized_body = body.strip()
    if not normalized_body or len(normalized_body) > 4096:
        raise ValueError("message body must contain 1 to 4096 characters")
    if not 1 <= len(idempotency_key) <= 160:
        raise ValueError("idempotency_key must contain 1 to 160 characters")

    with session.no_autoflush:
        tenant_result = await session.execute(
            select(Tenant)
            .where(Tenant.id == tenant_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    tenant = tenant_result.scalar_one_or_none()
    if tenant is None or tenant.status != "active":
        raise OutboundPolicyViolation(
            "tenant_inactive",
            "Outbound messages can only be queued for active businesses.",
        )

    existing_result = await session.execute(
        select(WhatsAppOutboundMessage).where(
            WhatsAppOutboundMessage.tenant_id == tenant_id,
            WhatsAppOutboundMessage.idempotency_key == idempotency_key,
        )
    )
    existing = existing_result.scalar_one_or_none()
    if existing is not None:
        if not _matches_request(
            existing,
            recipient=normalized_recipient,
            body=normalized_body,
            deliver_after_deactivation=deliver_after_deactivation,
        ):
            raise OutboundIdempotencyConflict(
                "Idempotency key was used with different message content"
            )
        return existing

    conversation = await assert_outbound_policy(
        session,
        tenant_id=tenant_id,
        recipient=normalized_recipient,
    )

    connection_result = await session.execute(
        select(WhatsAppConnection).where(
            WhatsAppConnection.tenant_id == tenant_id,
            WhatsAppConnection.status == "active",
        )
    )
    if connection_result.scalar_one_or_none() is None:
        raise ValueError("WhatsApp connection is not active")

    message = WhatsAppOutboundMessage(
        id=uuid4(),
        tenant_id=tenant_id,
        recipient_encrypted=encrypt_whatsapp_secret(normalized_recipient),
        body_encrypted=encrypt_whatsapp_secret(normalized_body),
        idempotency_key=idempotency_key,
        status="queued",
        deliver_after_deactivation=deliver_after_deactivation,
    )
    try:
        async with session.begin_nested():
            session.add(message)
            await session.flush()
    except IntegrityError:
        duplicate_result = await session.execute(
            select(WhatsAppOutboundMessage).where(
                WhatsAppOutboundMessage.tenant_id == tenant_id,
                WhatsAppOutboundMessage.idempotency_key == idempotency_key,
            )
        )
        duplicate = duplicate_result.scalar_one_or_none()
        if duplicate is None or not _matches_request(
            duplicate,
            recipient=normalized_recipient,
            body=normalized_body,
            deliver_after_deactivation=deliver_after_deactivation,
        ):
            raise OutboundIdempotencyConflict(
                "Idempotency key was concurrently used with different message content"
            ) from None
        return duplicate

    session.add(
        WhatsAppConversationMessage(
            tenant_id=tenant_id,
            conversation_id=conversation.id,
            provider_message_id=f"outbound:{message.id}",
            direction="outbound",
            message_type="text",
            body_encrypted=encrypt_whatsapp_secret(normalized_body),
            delivery_status="queued",
        )
    )
    session.add(
        AuditEvent(
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            event_type="whatsapp.message.queued",
            subject_type="whatsapp_outbound_message",
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
        job_type="whatsapp.send_text",
        payload={"message_id": str(message.id)},
        idempotency_key=(
            f"wa:outbound:{hashlib.sha256(idempotency_key.encode()).hexdigest()}"
        ),
        max_attempts=2,
    )
    await session.commit()
    return message
