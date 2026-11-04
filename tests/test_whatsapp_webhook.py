import asyncio
import hashlib
import hmac
import json
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.config import get_settings
from app.core.whatsapp import decrypt_whatsapp_secret
from app.db.session import AsyncSessionFactory
from app.jobs.queue import ClaimedJob
from app.jobs.whatsapp import accept_inbound_whatsapp_job
from app.main import app
from app.models import BackgroundJob, WhatsAppConnection, WhatsAppWebhookEvent


def test_whatsapp_webhook_is_verified_encrypted_and_idempotent(monkeypatch) -> None:
    monkeypatch.setenv("WHATSAPP_VERIFY_TOKEN", "test-verify-token")
    monkeypatch.setenv("WHATSAPP_APP_SECRET", "test-whatsapp-app-secret")
    monkeypatch.setenv(
        "WHATSAPP_TOKEN_ENCRYPTION_KEY",
        "c21lb3BzLWNpLXdoYXRzYXBwLWZlcm5ldC1rZXktMzI=",
    )
    monkeypatch.setenv("PLATFORM_ADMIN_KEY", "test-platform-admin-key-long-enough")
    get_settings.cache_clear()

    slug = f"wa-{uuid4().hex[:10]}"
    provider_message_id = f"wamid.{uuid4().hex}"
    access_token = "test-meta-access-token-with-enough-length"

    try:
        with TestClient(app) as client:
            registration = client.post(
                "/auth/register",
                json={
                    "business_name": "WhatsApp Test Business",
                    "tenant_slug": slug,
                    "email": f"{slug}@example.com",
                    "password": "Correct-Horse-Battery-42",
                    "business_timezone": "Africa/Lagos",
                },
            )
            assert registration.status_code == 201
            identity = client.get(
                "/auth/me",
                headers={"Authorization": f"Bearer {registration.json()['access_token']}"},
            )
            tenant_id = UUID(identity.json()["tenant_id"])

            provision = client.post(
                "/internal/whatsapp/connections",
                headers={"X-Platform-Admin-Key": "test-platform-admin-key-long-enough"},
                json={
                    "tenant_id": str(tenant_id),
                    "phone_number_id": "12345678901",
                    "business_account_id": "10987654321",
                    "access_token": access_token,
                },
            )
            assert provision.status_code == 200
            assert "access_token" not in provision.json()

            verification = client.get(
                "/webhooks/whatsapp",
                params={
                    "hub.mode": "subscribe",
                    "hub.verify_token": "test-verify-token",
                    "hub.challenge": "challenge-123",
                },
            )
            assert verification.status_code == 200
            assert verification.text == "challenge-123"

            payload = {
                "object": "whatsapp_business_account",
                "entry": [
                    {
                        "changes": [
                            {
                                "value": {
                                    "metadata": {"phone_number_id": "12345678901"},
                                    "messages": [
                                        {
                                            "from": "2348012345678",
                                            "id": provider_message_id,
                                            "timestamp": "1793433600",
                                            "type": "text",
                                            "text": {"body": "Hello SmeOps"},
                                        }
                                    ],
                                }
                            }
                        ]
                    }
                ],
            }
            body = json.dumps(payload, separators=(",", ":")).encode()
            signature = "sha256=" + hmac.new(
                b"test-whatsapp-app-secret",
                body,
                hashlib.sha256,
            ).hexdigest()

            invalid_signature = client.post(
                "/webhooks/whatsapp",
                content=body,
                headers={"X-Hub-Signature-256": "sha256=invalid"},
            )
            assert invalid_signature.status_code == 401

            first_delivery = client.post(
                "/webhooks/whatsapp",
                content=body,
                headers={"X-Hub-Signature-256": signature},
            )
            assert first_delivery.status_code == 200
            assert first_delivery.json()["queued_events"] == 1

            duplicate_delivery = client.post(
                "/webhooks/whatsapp",
                content=body,
                headers={"X-Hub-Signature-256": signature},
            )
            assert duplicate_delivery.status_code == 200
            assert duplicate_delivery.json()["queued_events"] == 0

        async def inspect_persisted_records():
            async with AsyncSessionFactory() as session:
                connection_result = await session.execute(
                    select(WhatsAppConnection).where(
                        WhatsAppConnection.tenant_id == tenant_id
                    )
                )
                connection = connection_result.scalar_one()

                event_result = await session.execute(
                    select(WhatsAppWebhookEvent).where(
                        WhatsAppWebhookEvent.tenant_id == tenant_id
                    )
                )
                event = event_result.scalar_one()

                job_result = await session.execute(
                    select(BackgroundJob).where(
                        BackgroundJob.tenant_id == tenant_id,
                        BackgroundJob.job_type == "whatsapp.accept_inbound",
                    )
                )
                job = job_result.scalar_one()
                return connection, event, job

        connection, event, job = asyncio.run(inspect_persisted_records())
        persisted_job_id = job.id
        assert connection.access_token_encrypted != access_token
        assert decrypt_whatsapp_secret(connection.access_token_encrypted) == access_token
        assert "Hello SmeOps" not in event.payload_encrypted
        assert event.status == "queued"
        assert job.status == "queued"

        async def process_one_job():
            claimed = ClaimedJob(
                id=job.id,
                tenant_id=tenant_id,
                job_type=job.job_type,
                payload=job.payload,
                attempts=1,
                max_attempts=job.max_attempts,
            )
            await accept_inbound_whatsapp_job(claimed)

        asyncio.run(process_one_job())

        async def inspect_processed_event():
            async with AsyncSessionFactory() as session:
                event_result = await session.execute(
                    select(WhatsAppWebhookEvent).where(
                        WhatsAppWebhookEvent.tenant_id == tenant_id
                    )
                )
                event = event_result.scalar_one()
                job_result = await session.execute(
                    select(BackgroundJob).where(BackgroundJob.id == persisted_job_id)
                )
                job = job_result.scalar_one()
                return event.status, job.status

        event_status, job_status = asyncio.run(inspect_processed_event())
        assert event_status == "accepted"
        assert job_status == "queued"
    finally:
        get_settings.cache_clear()
