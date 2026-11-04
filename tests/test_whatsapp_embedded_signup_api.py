import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.config import get_settings
from app.core.whatsapp import decrypt_whatsapp_secret
from app.db.session import AsyncSessionFactory
from app.integrations.meta_whatsapp import MetaAccessToken, MetaGraphAPIError, MetaPhoneNumber
from app.main import app
from app.models import AuditEvent, Tenant, WhatsAppConnection


def test_embedded_signup_persists_encrypted_connection_and_supports_retry(monkeypatch):
    monkeypatch.setenv("META_APP_ID", "123456789012345")
    monkeypatch.setenv("META_APP_SECRET", "test-meta-app-secret")
    monkeypatch.setenv("WHATSAPP_GRAPH_API_VERSION", "v26.0")
    monkeypatch.setenv(
        "WHATSAPP_TOKEN_ENCRYPTION_KEY",
        "c21lb3BzLWNpLXdoYXRzYXBwLWZlcm5ldC1rZXktMzI=",
    )
    get_settings.cache_clear()

    calls = {"register": 0}

    async def fake_exchange(**_kwargs):
        return MetaAccessToken(
            value="test-business-integration-token-123456",
            expires_at=datetime.now(UTC) + timedelta(days=60),
        )

    async def fake_verify(**_kwargs):
        return MetaPhoneNumber(
            display_phone_number="+234 801 234 5678",
            verified_name="Studio Ada",
        )

    async def fake_subscribe(**_kwargs):
        return None

    async def fake_register(**_kwargs):
        calls["register"] += 1
        if calls["register"] == 1:
            raise MetaGraphAPIError("meta_graph_100", 400)

    monkeypatch.setattr("app.api.whatsapp.exchange_embedded_signup_code", fake_exchange)
    monkeypatch.setattr(
        "app.services.whatsapp_onboarding.verify_phone_number_belongs_to_waba",
        fake_verify,
    )
    monkeypatch.setattr(
        "app.services.whatsapp_onboarding.subscribe_app_to_waba",
        fake_subscribe,
    )
    monkeypatch.setattr(
        "app.services.whatsapp_onboarding.register_phone_number",
        fake_register,
    )

    slug = f"meta-signup-{uuid4().hex[:10]}"
    tenant_id: UUID | None = None
    try:
        with TestClient(app) as client:
            registration = client.post(
                "/auth/register",
                json={
                    "business_name": "Studio Ada",
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

            request_body = {
                "code": "single-use-meta-code-12345",
                "waba_id": "987654321098",
                "phone_number_id": "123456789012",
                "two_step_pin": "123456",
            }
            incomplete = client.post(
                "/whatsapp/embedded-signup",
                headers=auth,
                json=request_body,
            )
            assert incomplete.status_code == 502

            pending = client.get("/whatsapp/connection", headers=auth)
            assert pending.status_code == 200
            assert pending.json()["status"] == "pending"
            assert pending.json()["onboarding_error_code"] == "meta_graph_100"
            assert "access_token" not in pending.json()

            completed = client.post(
                "/whatsapp/connection/complete",
                headers=auth,
                json={},
            )
            assert completed.status_code == 200
            assert completed.json()["status"] == "active"
            assert completed.json()["display_phone_number"] == "+234 801 234 5678"
            assert completed.json()["verified_name"] == "Studio Ada"
            assert "access_token" not in completed.json()
            assert "two_step_pin" not in completed.json()

            async def inspect_persistence():
                async with AsyncSessionFactory() as session:
                    result = await session.execute(
                        select(WhatsAppConnection).where(
                            WhatsAppConnection.tenant_id == tenant_id
                        )
                    )
                    connection = result.scalar_one()
                    events = await session.execute(
                        select(AuditEvent).where(
                            AuditEvent.tenant_id == tenant_id,
                            AuditEvent.event_type.in_(
                                [
                                    "whatsapp.connection.onboarding_started",
                                    "whatsapp.connection.onboarding_failed",
                                    "whatsapp.connection.connected",
                                ]
                            ),
                        )
                    )
                    return (
                        connection.status,
                        decrypt_whatsapp_secret(connection.access_token_encrypted),
                        decrypt_whatsapp_secret(
                            connection.two_step_verification_pin_encrypted
                        ),
                        {event.event_type for event in events.scalars().all()},
                    )

            status_value, stored_token, stored_pin, event_types = asyncio.run(
                inspect_persistence()
            )
            assert status_value == "active"
            assert stored_token == "test-business-integration-token-123456"
            assert stored_pin == "123456"
            assert {
                "whatsapp.connection.onboarding_started",
                "whatsapp.connection.onboarding_failed",
                "whatsapp.connection.connected",
            }.issubset(event_types)

        async def cleanup():
            async with AsyncSessionFactory() as session:
                tenant = await session.get(Tenant, tenant_id)
                if tenant is not None:
                    await session.delete(tenant)
                    await session.commit()

        asyncio.run(cleanup())
    finally:
        get_settings.cache_clear()
