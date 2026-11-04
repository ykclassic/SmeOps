import asyncio
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.config import get_settings
from app.db.session import AsyncSessionFactory
from app.main import app
from app.models import BackgroundJob, Tenant, WhatsAppOutboundMessage


def test_authenticated_owner_can_queue_tenant_scoped_whatsapp_message(monkeypatch):
    monkeypatch.setenv(
        "WHATSAPP_TOKEN_ENCRYPTION_KEY",
        "c21lb3BzLWNpLXdoYXRzYXBwLWZlcm5ldC1rZXktMzI=",
    )
    monkeypatch.setenv("PLATFORM_ADMIN_KEY", "test-platform-admin-key-long-enough")
    get_settings.cache_clear()
    slug = f"send-{uuid4().hex[:10]}"

    try:
        with TestClient(app) as client:
            registration = client.post(
                "/auth/register",
                json={
                    "business_name": "Send Test Salon",
                    "tenant_slug": slug,
                    "email": f"{slug}@example.com",
                    "password": "Correct-Horse-Battery-42",
                    "business_timezone": "Africa/Lagos",
                },
            )
            assert registration.status_code == 201
            token = registration.json()["access_token"]
            auth = {"Authorization": f"Bearer {token}"}
            identity = client.get("/auth/me", headers=auth)
            tenant_id = UUID(identity.json()["tenant_id"])

            provision = client.post(
                "/internal/whatsapp/connections",
                headers={"X-Platform-Admin-Key": "test-platform-admin-key-long-enough"},
                json={
                    "tenant_id": str(tenant_id),
                    "phone_number_id": "12345678901",
                    "business_account_id": "10987654321",
                    "access_token": "test-meta-access-token-with-enough-length",
                },
            )
            assert provision.status_code == 200

            unauthorized = client.post(
                "/whatsapp/messages",
                json={
                    "recipient": "+2348012345678",
                    "body": "Your booking is confirmed.",
                    "idempotency_key": str(uuid4()),
                },
            )
            assert unauthorized.status_code == 401

            response = client.post(
                "/whatsapp/messages",
                headers=auth,
                json={
                    "recipient": "+2348012345678",
                    "body": "Your booking is confirmed.",
                    "idempotency_key": str(uuid4()),
                },
            )
            assert response.status_code == 202
            assert response.json()["status"] == "queued"
            message_id = UUID(response.json()["id"])

        async def inspect_records():
            async with AsyncSessionFactory() as session:
                message = await session.get(WhatsAppOutboundMessage, message_id)
                jobs = await session.execute(
                    select(BackgroundJob).where(
                        BackgroundJob.tenant_id == tenant_id,
                        BackgroundJob.job_type == "whatsapp.send_text",
                    )
                )
                return message, jobs.scalar_one()

        message, job = asyncio.run(inspect_records())
        assert message is not None
        assert message.tenant_id == tenant_id
        assert job.tenant_id == tenant_id
        assert job.status == "queued"
        assert job.max_attempts == 2

        async def cleanup():
            async with AsyncSessionFactory() as session:
                tenant = await session.get(Tenant, tenant_id)
                await session.delete(tenant)
                await session.commit()

        asyncio.run(cleanup())
    finally:
        get_settings.cache_clear()
