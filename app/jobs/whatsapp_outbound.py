import logging
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select

from app.core.config import get_settings
from app.core.whatsapp import decrypt_whatsapp_secret
from app.db.session import AsyncSessionFactory
from app.integrations.whatsapp_cloud import (
    WhatsAppDeliveryUnknown,
    WhatsAppSendRejected,
    send_text_message,
)
from app.jobs.queue import ClaimedJob
from app.models import WhatsAppConnection, WhatsAppOutboundMessage
from app.services.whatsapp_outbound import (
    OutboundPolicyViolation,
    assert_outbound_policy,
)

logger = logging.getLogger(__name__)


async def send_outbound_whatsapp_job(job: ClaimedJob) -> None:
    try:
        message_id = UUID(job.payload["message_id"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("Invalid outbound WhatsApp job payload") from exc

    async with AsyncSessionFactory() as session:
        result = await session.execute(
            select(WhatsAppOutboundMessage)
            .where(
                WhatsAppOutboundMessage.id == message_id,
                WhatsAppOutboundMessage.tenant_id == job.tenant_id,
            )
            .with_for_update()
        )
        message = result.scalar_one_or_none()
        if message is None:
            raise ValueError("Outbound WhatsApp message does not exist")
        if message.status in {"sent", "delivered", "read", "failed", "unknown"}:
            return
        if message.status == "sending":
            # A previous worker may have sent the request before crashing.
            # Never retry an external write with an ambiguous outcome.
            message.status = "unknown"
            message.last_error_code = "previous_send_outcome_unknown"
            await session.commit()
            return
        if message.status != "queued":
            raise ValueError("Outbound WhatsApp message is not in a sendable state")

        connection_result = await session.execute(
            select(WhatsAppConnection).where(
                WhatsAppConnection.tenant_id == job.tenant_id,
                WhatsAppConnection.status == "active",
            )
        )
        connection = connection_result.scalar_one_or_none()
        if connection is None:
            message.status = "failed"
            message.last_error_code = "connection_unavailable"
            await session.commit()
            return

        settings = get_settings()
        if settings.whatsapp_graph_api_version is None:
            message.status = "failed"
            message.last_error_code = "graph_api_version_not_configured"
            await session.commit()
            return

        recipient = decrypt_whatsapp_secret(message.recipient_encrypted)
        body = decrypt_whatsapp_secret(message.body_encrypted)
        try:
            await assert_outbound_policy(
                session,
                tenant_id=job.tenant_id,
                recipient=recipient,
            )
        except OutboundPolicyViolation as exc:
            message.status = "failed"
            message.last_error_code = exc.error_code
            await session.commit()
            return

        access_token = decrypt_whatsapp_secret(connection.access_token_encrypted)
        phone_number_id = connection.phone_number_id
        message.status = "sending"
        await session.commit()

    try:
        result = await send_text_message(
            api_version=settings.whatsapp_graph_api_version,
            phone_number_id=phone_number_id,
            access_token=access_token,
            recipient=recipient,
            body=body,
        )
    except WhatsAppSendRejected:
        new_status = "failed"
        error_code = "provider_rejected"
        provider_message_id = None
    except WhatsAppDeliveryUnknown:
        new_status = "unknown"
        error_code = "provider_outcome_unknown"
        provider_message_id = None
    else:
        new_status = "sent"
        error_code = None
        provider_message_id = result.provider_message_id

    async with AsyncSessionFactory() as session:
        result = await session.execute(
            select(WhatsAppOutboundMessage)
            .where(
                WhatsAppOutboundMessage.id == message_id,
                WhatsAppOutboundMessage.tenant_id == job.tenant_id,
            )
            .with_for_update()
        )
        message = result.scalar_one_or_none()
        if message is None:
            logger.error(
                "Outbound WhatsApp ledger row disappeared",
                extra={"message_id": str(message_id)},
            )
            return
        if message.status not in {"sending", "unknown"}:
            logger.warning(
                "Outbound WhatsApp state changed during provider request",
                extra={"message_id": str(message_id), "status": message.status},
            )
            return
        message.status = new_status
        message.last_error_code = error_code
        message.provider_message_id = provider_message_id
        if new_status == "sent":
            message.sent_at = datetime.now(UTC)
        await session.commit()
