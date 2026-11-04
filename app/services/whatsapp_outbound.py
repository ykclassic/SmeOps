import hashlib
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.whatsapp import encrypt_whatsapp_secret
from app.jobs.queue import enqueue_job
from app.models import WhatsAppConnection, WhatsAppOutboundMessage


class OutboundIdempotencyConflict(ValueError):
    """An outbound idempotency key was reused for different content."""


async def enqueue_whatsapp_text(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    recipient: str,
    body: str,
    idempotency_key: str,
) -> WhatsAppOutboundMessage:
    normalized_recipient = recipient.strip().lstrip("+")
    if not normalized_recipient.isdigit() or not 8 <= len(normalized_recipient) <= 15:
        raise ValueError("recipient must be an international phone number")
    normalized_body = body.strip()
    if not normalized_body or len(normalized_body) > 4096:
        raise ValueError("message body must contain 1 to 4096 characters")

    existing_result = await session.execute(
        select(WhatsAppOutboundMessage).where(
            WhatsAppOutboundMessage.tenant_id == tenant_id,
            WhatsAppOutboundMessage.idempotency_key == idempotency_key,
        )
    )
    existing = existing_result.scalar_one_or_none()
    if existing is not None:
        from app.core.whatsapp import decrypt_whatsapp_secret

        same_recipient = decrypt_whatsapp_secret(existing.recipient_encrypted) == normalized_recipient
        same_body = decrypt_whatsapp_secret(existing.body_encrypted) == normalized_body
        if not (same_recipient and same_body):
            raise OutboundIdempotencyConflict(
                "Idempotency key was used with different message content"
            )
        return existing

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
    )
    session.add(message)
    await session.flush()
    await enqueue_job(
        session,
        tenant_id=tenant_id,
        job_type="whatsapp.send_text",
        payload={"message_id": str(message.id)},
        idempotency_key=f"wa:outbound:{hashlib.sha256(idempotency_key.encode()).hexdigest()}",
        max_attempts=1,
    )
    await session.commit()
    return message
