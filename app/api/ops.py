from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, status
from sqlalchemy import func, select

from app.api.dependencies import DbSession
from app.core.config import get_settings
from app.models import (
    BackgroundJob,
    WhatsAppConnection,
    WhatsAppOutboundMessage,
    WhatsAppWebhookEvent,
    WorkerHeartbeat,
)

router = APIRouter(prefix="/internal/ops", tags=["internal operations"])


@router.get("/summary")
async def operations_summary(
    session: DbSession,
    admin_key: Annotated[str | None, Header(alias="X-Platform-Admin-Key")] = None,
) -> dict:
    settings = get_settings()
    expected_key = settings.platform_admin_key
    if expected_key is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Operations access is not configured",
        )
    if admin_key is None or not __import__("hmac").compare_digest(
        admin_key.encode(),
        expected_key.get_secret_value().encode(),
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized",
        )

    now = datetime.now(UTC)
    queued_result = await session.execute(
        select(func.count(BackgroundJob.id), func.min(BackgroundJob.created_at)).where(
            BackgroundJob.status == "queued"
        )
    )
    queued_count, oldest_queued_at = queued_result.one()
    status_result = await session.execute(
        select(BackgroundJob.status, func.count(BackgroundJob.id)).group_by(
            BackgroundJob.status
        )
    )
    job_counts = {name: count for name, count in status_result.all()}

    worker_result = await session.execute(
        select(WorkerHeartbeat).order_by(WorkerHeartbeat.last_seen_at.desc())
    )
    workers = [
        {
            "worker_id": worker.worker_id,
            "status": (
                "healthy"
                if worker.status == "running"
                and worker.last_seen_at >= now - timedelta(seconds=90)
                else "stale"
                if worker.status == "running"
                else "stopped"
            ),
            "last_seen_at": worker.last_seen_at.isoformat(),
        }
        for worker in worker_result.scalars().all()
    ]

    outbound_result = await session.execute(
        select(WhatsAppOutboundMessage.status, func.count(WhatsAppOutboundMessage.id))
        .group_by(WhatsAppOutboundMessage.status)
    )
    outbound_counts = {name: count for name, count in outbound_result.all()}

    webhook_result = await session.execute(
        select(WhatsAppWebhookEvent.status, func.count(WhatsAppWebhookEvent.id))
        .group_by(WhatsAppWebhookEvent.status)
    )
    webhook_counts = {name: count for name, count in webhook_result.all()}

    token_result = await session.execute(
        select(func.count(WhatsAppConnection.id)).where(
            WhatsAppConnection.status == "active",
            WhatsAppConnection.token_expires_at.is_not(None),
            WhatsAppConnection.token_expires_at <= now + timedelta(days=7),
        )
    )
    expiring_tokens = token_result.scalar_one()

    return {
        "generated_at": now.isoformat(),
        "jobs": {
            "queued": job_counts.get("queued", 0),
            "running": job_counts.get("running", 0),
            "completed": job_counts.get("completed", 0),
            "dead_letter": job_counts.get("dead_letter", 0),
            "oldest_queued_at": (
                oldest_queued_at.isoformat() if oldest_queued_at is not None else None
            ),
            "oldest_queued_age_seconds": (
                max(0, int((now - oldest_queued_at).total_seconds()))
                if oldest_queued_at is not None
                else 0
            ),
        },
        "workers": workers,
        "outbound_messages": outbound_counts,
        "webhook_events": webhook_counts,
        "whatsapp_tokens_expiring_within_7_days": expiring_tokens,
    }
