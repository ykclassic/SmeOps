from uuid import UUID

from sqlalchemy import select

from app.core.config import get_settings
from app.db.session import AsyncSessionFactory
from app.jobs.queue import ClaimedJob
from app.models import WhatsAppConnection
from app.services.whatsapp_onboarding import complete_pending_whatsapp_connection


async def complete_whatsapp_onboarding_job(job: ClaimedJob) -> None:
    connection_id_value = job.payload.get("connection_id")
    if not isinstance(connection_id_value, str):
        raise ValueError("WhatsApp onboarding job is missing its connection ID")
    connection_id = UUID(connection_id_value)
    settings = get_settings()
    if settings.whatsapp_graph_api_version is None:
        raise RuntimeError("WhatsApp Graph API version is not configured")

    async with AsyncSessionFactory() as session:
        result = await session.execute(
            select(WhatsAppConnection)
            .where(
                WhatsAppConnection.id == connection_id,
                WhatsAppConnection.tenant_id == job.tenant_id,
                WhatsAppConnection.status == "pending",
            )
            .with_for_update()
        )
        connection = result.scalar_one_or_none()
        if connection is None:
            return
        await complete_pending_whatsapp_connection(
            session,
            connection=connection,
            actor_user_id=None,
            api_version=settings.whatsapp_graph_api_version,
        )
