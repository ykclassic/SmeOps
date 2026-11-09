from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.jobs.queue import enqueue_job
from app.models import TelegramOwnerLink, TelegramOwnerNotification, User


async def enqueue_appointment_owner_alerts(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    appointment_id: UUID,
    event_type: str = "appointment.created",
    event_key: str | None = None,
) -> int:
    """Queue one durable, tenant-scoped alert for each lifecycle event and owner."""
    resolved_event_key = event_key or f"{event_type}:{appointment_id}"
    if not 1 <= len(resolved_event_key) <= 160:
        raise ValueError("Invalid Telegram owner notification event key")
    links_result = await session.execute(
        select(TelegramOwnerLink)
        .join(
            User,
            (User.id == TelegramOwnerLink.owner_user_id)
            & (User.tenant_id == TelegramOwnerLink.tenant_id),
        )
        .where(
            User.status == "active",
            User.role == "owner",
            TelegramOwnerLink.tenant_id == tenant_id,
            TelegramOwnerLink.linked_at.is_not(None),
            TelegramOwnerLink.disabled_at.is_(None),
            TelegramOwnerLink.chat_id.is_not(None),
        )
    )
    links = list(links_result.scalars().all())
    queued = 0

    for link in links:
        existing_result = await session.execute(
            select(TelegramOwnerNotification).where(
                TelegramOwnerNotification.tenant_id == tenant_id,
                TelegramOwnerNotification.owner_user_id == link.owner_user_id,
                TelegramOwnerNotification.event_key == resolved_event_key,
            )
        )
        if existing_result.scalar_one_or_none() is not None:
            continue

        notification = TelegramOwnerNotification(
            tenant_id=tenant_id,
            owner_user_id=link.owner_user_id,
            appointment_id=appointment_id,
            event_type=event_type,
            event_key=resolved_event_key,
            status="queued",
        )
        session.add(notification)
        await session.flush()
        await enqueue_job(
            session,
            tenant_id=tenant_id,
            job_type="telegram.owner_appointment_alert",
            payload={"notification_id": str(notification.id)},
            idempotency_key=(
                f"tg:owner-alert:{resolved_event_key}:{link.owner_user_id}"
            ),
            max_attempts=3,
        )
        queued += 1
    return queued
