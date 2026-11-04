import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.core.whatsapp import encrypt_whatsapp_secret, hash_whatsapp_contact
from app.db.session import AsyncSessionFactory
from app.main import app
from app.models import Tenant, WhatsAppConversation, WhatsAppConversationMessage


def test_conversation_inbox_is_authenticated_and_tenant_scoped(monkeypatch):
    monkeypatch.setenv(
        "WHATSAPP_TOKEN_ENCRYPTION_KEY",
        "c21lb3BzLWNpLXdoYXRzYXBwLWZlcm5ldC1rZXktMzI=",
    )
    get_settings.cache_clear()
    tenant_a_id: UUID | None = None
    tenant_b_id: UUID | None = None

    try:
        with TestClient(app) as client:
            registration_a = client.post(
                "/auth/register",
                json={
                    "business_name": "Inbox Salon A",
                    "tenant_slug": f"inbox-a-{uuid4().hex[:10]}",
                    "email": f"inbox-a-{uuid4().hex[:10]}@example.com",
                    "password": "Correct-Horse-Battery-42",
                    "business_timezone": "Africa/Lagos",
                },
            )
            registration_b = client.post(
                "/auth/register",
                json={
                    "business_name": "Inbox Salon B",
                    "tenant_slug": f"inbox-b-{uuid4().hex[:10]}",
                    "email": f"inbox-b-{uuid4().hex[:10]}@example.com",
                    "password": "Correct-Horse-Battery-42",
                    "business_timezone": "Africa/Lagos",
                },
            )
            assert registration_a.status_code == 201
            assert registration_b.status_code == 201

            token_a = registration_a.json()["access_token"]
            token_b = registration_b.json()["access_token"]
            tenant_a_id = UUID(
                client.get(
                    "/auth/me",
                    headers={"Authorization": f"Bearer {token_a}"},
                ).json()["tenant_id"]
            )
            tenant_b_id = UUID(
                client.get(
                    "/auth/me",
                    headers={"Authorization": f"Bearer {token_b}"},
                ).json()["tenant_id"]
            )

            async def create_inbox_records():
                async with AsyncSessionFactory() as session:
                    conversation = WhatsAppConversation(
                        tenant_id=tenant_a_id,
                        customer_phone_hash=hash_whatsapp_contact("2348012345678"),
                        customer_phone_encrypted=encrypt_whatsapp_secret("2348012345678"),
                        status="active",
                        context={},
                        last_inbound_at=datetime.now(UTC),
                    )
                    session.add(conversation)
                    await session.flush()
                    session.add_all(
                        [
                            WhatsAppConversationMessage(
                                tenant_id=tenant_a_id,
                                conversation_id=conversation.id,
                                provider_message_id="wamid.inbox-test-1",
                                direction="inbound",
                                message_type="text",
                                body_encrypted=encrypt_whatsapp_secret("I need a haircut"),
                                provider_timestamp=datetime.now(UTC) - timedelta(minutes=1),
                                delivery_status="received",
                            ),
                            WhatsAppConversationMessage(
                                tenant_id=tenant_a_id,
                                conversation_id=conversation.id,
                                provider_message_id="wamid.inbox-test-2",
                                direction="inbound",
                                message_type="text",
                                body_encrypted=encrypt_whatsapp_secret("Can I book today?"),
                                provider_timestamp=datetime.now(UTC),
                                delivery_status="received",
                            ),
                        ]
                    )
                    await session.commit()
                    return conversation.id

            conversation_id = asyncio.run(create_inbox_records())
            auth_a = {"Authorization": f"Bearer {token_a}"}
            auth_b = {"Authorization": f"Bearer {token_b}"}

            unauthenticated = client.get("/whatsapp/conversations")
            assert unauthenticated.status_code == 401

            list_a = client.get("/whatsapp/conversations", headers=auth_a)
            assert list_a.status_code == 200
            assert len(list_a.json()) == 1
            assert list_a.json()[0]["id"] == str(conversation_id)
            assert list_a.json()[0]["customer_phone"] == "2348012345678"

            messages_a = client.get(
                f"/whatsapp/conversations/{conversation_id}/messages",
                headers=auth_a,
            )
            assert messages_a.status_code == 200
            assert {item["body"] for item in messages_a.json()} == {
                "I need a haircut",
                "Can I book today?",
            }

            paged_messages = client.get(
                f"/whatsapp/conversations/{conversation_id}/messages?limit=1",
                headers=auth_a,
            )
            assert paged_messages.status_code == 200
            assert len(paged_messages.json()) == 1

            list_b = client.get("/whatsapp/conversations", headers=auth_b)
            assert list_b.status_code == 200
            assert list_b.json() == []

            cross_tenant = client.get(
                f"/whatsapp/conversations/{conversation_id}/messages",
                headers=auth_b,
            )
            assert cross_tenant.status_code == 404

        async def cleanup():
            async with AsyncSessionFactory() as session:
                for tenant_id in (tenant_a_id, tenant_b_id):
                    tenant = await session.get(Tenant, tenant_id)
                    if tenant is not None:
                        await session.delete(tenant)
                await session.commit()

        asyncio.run(cleanup())
    finally:
        get_settings.cache_clear()
