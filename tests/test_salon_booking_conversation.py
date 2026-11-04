import asyncio
import hashlib
import hmac
import json
from datetime import UTC, datetime
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.config import get_settings
from app.core.whatsapp import decrypt_whatsapp_secret
from app.db.session import AsyncSessionFactory
from app.jobs.queue import ClaimedJob
from app.jobs.whatsapp import accept_inbound_whatsapp_job
from app.main import app
from app.models import (
    Appointment,
    BackgroundJob,
    Tenant,
    WhatsAppConversationMessage,
    WhatsAppOutboundMessage,
)
from app.services.whatsapp_conversation import process_whatsapp_conversation_job


def test_complete_salon_booking_conversation(monkeypatch):
    monkeypatch.setenv("WHATSAPP_VERIFY_TOKEN", "test-verify-token")
    monkeypatch.setenv("WHATSAPP_APP_SECRET", "test-whatsapp-app-secret")
    monkeypatch.setenv(
        "WHATSAPP_TOKEN_ENCRYPTION_KEY",
        "c21lb3BzLWNpLXdoYXRzYXBwLWZlcm5ldC1rZXktMzI=",
    )
    monkeypatch.setenv("PLATFORM_ADMIN_KEY", "test-platform-admin-key-long-enough")
    get_settings.cache_clear()

    slug = f"salon-flow-{uuid4().hex[:10]}"
    phone = "2348012345678"
    tenant_id: UUID | None = None

    try:
        with TestClient(app) as client:
            registration = client.post(
                "/auth/register",
                json={
                    "business_name": "End-to-End Salon",
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

            service = client.post(
                "/services",
                headers=auth,
                json={
                    "name": "Haircut",
                    "description": "Wash and haircut",
                    "duration_minutes": 60,
                    "price_minor": 2500000,
                },
            )
            assert service.status_code == 201

            hours = client.put(
                "/business-hours",
                headers=auth,
                json={
                    "days": [
                        {
                            "weekday": weekday,
                            "is_closed": False,
                            "opens_at": "08:00:00",
                            "closes_at": "18:00:00",
                        }
                        for weekday in range(7)
                    ]
                },
            )
            assert hours.status_code == 200

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

            async def find_job(job_type: str) -> ClaimedJob:
                async with AsyncSessionFactory() as session:
                    result = await session.execute(
                        select(BackgroundJob)
                        .where(
                            BackgroundJob.tenant_id == tenant_id,
                            BackgroundJob.job_type == job_type,
                        )
                        .order_by(BackgroundJob.created_at.desc())
                        .limit(1)
                    )
                    job = result.scalar_one()
                    return ClaimedJob(
                        id=job.id,
                        tenant_id=job.tenant_id,
                        job_type=job.job_type,
                        payload=job.payload,
                        attempts=1,
                        max_attempts=job.max_attempts,
                    )

            def send_customer_message(text: str) -> None:
                provider_id = f"wamid.{uuid4().hex}"
                payload = {
                    "object": "whatsapp_business_account",
                    "entry": [{
                        "changes": [{
                            "value": {
                                "metadata": {"phone_number_id": "12345678901"},
                                "messages": [{
                                    "from": phone,
                                    "id": provider_id,
                                    "timestamp": str(int(datetime.now(UTC).timestamp())),
                                    "type": "text",
                                    "text": {"body": text},
                                }],
                            }
                        }]
                    }],
                }
                raw = json.dumps(payload, separators=(",", ":")).encode()
                signature = "sha256=" + hmac.new(
                    b"test-whatsapp-app-secret", raw, hashlib.sha256
                ).hexdigest()
                response = client.post(
                    "/webhooks/whatsapp",
                    content=raw,
                    headers={"X-Hub-Signature-256": signature},
                )
                assert response.status_code == 200
                assert response.json()["queued_events"] == 1

            async def process_latest_inbound():
                inbound_job = await find_job("whatsapp.accept_inbound")
                await accept_inbound_whatsapp_job(inbound_job)
                conversation_job = await find_job("whatsapp.process_conversation")
                await process_whatsapp_conversation_job(conversation_job)

            for customer_text in ("BOOK", "1", "Ada Lovelace", "tomorrow", "1", "YES"):
                send_customer_message(customer_text)
                asyncio.run(process_latest_inbound())

            async def inspect_results():
                async with AsyncSessionFactory() as session:
                    appointments_result = await session.execute(
                        select(Appointment).where(Appointment.tenant_id == tenant_id)
                    )
                    appointments = list(appointments_result.scalars().all())
                    outbound_result = await session.execute(
                        select(WhatsAppOutboundMessage).where(
                            WhatsAppOutboundMessage.tenant_id == tenant_id
                        )
                    )
                    outbound = list(outbound_result.scalars().all())
                    history_result = await session.execute(
                        select(WhatsAppConversationMessage).where(
                            WhatsAppConversationMessage.tenant_id == tenant_id,
                            WhatsAppConversationMessage.direction == "outbound",
                        )
                    )
                    history = list(history_result.scalars().all())
                    return appointments, outbound, history

            appointments, outbound, history = asyncio.run(inspect_results())
            assert len(appointments) == 1
            assert appointments[0].status == "confirmed"
            assert len(outbound) == 6
            assert len(history) == 6
            assert any(
                "appointment is confirmed"
                in decrypt_whatsapp_secret(item.body_encrypted).casefold()
                for item in history
            )

            inbox = client.get("/whatsapp/conversations", headers=auth)
            assert inbox.status_code == 200
            conversation_id = inbox.json()[0]["id"]
            messages = client.get(
                f"/whatsapp/conversations/{conversation_id}/messages",
                headers=auth,
            )
            assert messages.status_code == 200
            assert len(messages.json()) == 12

        async def cleanup():
            async with AsyncSessionFactory() as session:
                tenant = await session.get(Tenant, tenant_id)
                if tenant is not None:
                    await session.delete(tenant)
                    await session.commit()

        asyncio.run(cleanup())
    finally:
        get_settings.cache_clear()
