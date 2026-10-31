from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select

from app.db.session import AsyncSessionFactory
from app.jobs.queue import ClaimedJob
from app.models import WhatsAppWebhookEvent


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

        event.status = "accepted"
        event.processed_at = None
        await session.commit()
