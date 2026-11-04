import json
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select

from app.core.whatsapp import decrypt_whatsapp_secret
from app.db.session import AsyncSessionFactory
from app.jobs.queue import ClaimedJob
from app.models import WhatsAppOutboundMessage, WhatsAppWebhookEvent


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

        if event.event_type == "message.status":
            payload = json.loads(decrypt_whatsapp_secret(event.payload_encrypted))
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
            message = message_result.scalar_one_or_none()
            if message is None:
                # Keep this event retryable: the send worker may not yet have
                # committed the provider message ID returned by Meta.
                raise RuntimeError("Outbound message not yet correlated to provider status")

            message.status = message_status
            if message_status == "delivered":
                message.delivered_at = datetime.now(UTC)
            elif message_status == "read":
                message.read_at = datetime.now(UTC)
            elif message_status == "failed":
                message.last_error_code = "provider_delivery_failed"

            event.status = "processed"
            event.processed_at = datetime.now(UTC)
            await session.commit()
            return

        event.status = "accepted"
        event.processed_at = None
        await session.commit()
