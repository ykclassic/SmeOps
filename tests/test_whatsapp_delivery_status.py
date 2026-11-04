import hashlib
import json
from uuid import uuid4

import pytest

from app.core.config import get_settings
from app.core.whatsapp import encrypt_whatsapp_secret
from app.db.session import AsyncSessionFactory
from app.jobs.queue import ClaimedJob
from app.jobs.whatsapp import accept_inbound_whatsapp_job
from app.models import (
    Tenant,
    WhatsAppConnection,
    WhatsAppOutboundMessage,
    WhatsAppWebhookEvent,
)


@pytest.fixture(autouse=True)
def whatsapp_settings(monkeypatch):
    monkeypatch.setenv(
        "WHATSAPP_TOKEN_ENCRYPTION_KEY",
        "c21lb3BzLWNpLXdoYXRzYXBwLWZlcm5ldC1rZXktMzI=",
    )
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def create_outbound_message():
    tenant = Tenant(
        id=uuid4(),
        name="Delivery Status Test Business",
        slug=f"delivery-{uuid4().hex[:12]}",
        business_timezone="Africa/Lagos",
        status="active",
    )
    connection = WhatsAppConnection(
        tenant_id=tenant.id,
        phone_number_id=f"{uuid4().int % 10**11:011d}",
        business_account_id=f"{uuid4().int % 10**11:011d}",
        access_token_encrypted=encrypt_whatsapp_secret("test-meta-access-token"),
        status="active",
    )
    message = WhatsAppOutboundMessage(
        tenant_id=tenant.id,
        recipient_encrypted=encrypt_whatsapp_secret("2348012345678"),
        body_encrypted=encrypt_whatsapp_secret("Test delivery status"),
        idempotency_key=f"status-test:{uuid4()}",
        status="sent",
        provider_message_id=f"wamid.{uuid4().hex}",
    )
    async with AsyncSessionFactory() as session:
        session.add_all([tenant, connection, message])
        await session.commit()
    return tenant, message


async def add_status_event(tenant_id, provider_message_id, status_name):
    payload = {
        "metadata": {"phone_number_id": "12345678901"},
        "status": {"id": provider_message_id, "status": status_name},
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    event = WhatsAppWebhookEvent(
        id=uuid4(),
        tenant_id=tenant_id,
        provider_event_id=f"status:{provider_message_id}:{status_name}",
        event_type="message.status",
        payload_encrypted=encrypt_whatsapp_secret(canonical),
        payload_sha256=hashlib.sha256(canonical.encode()).hexdigest(),
        status="queued",
    )
    async with AsyncSessionFactory() as session:
        session.add(event)
        await session.commit()
    return event


@pytest.mark.asyncio
async def test_delivery_status_is_reconciled_and_never_regresses():
    tenant, message = await create_outbound_message()
    read_event = await add_status_event(tenant.id, message.provider_message_id, "read")
    read_job = ClaimedJob(
        id=uuid4(),
        tenant_id=tenant.id,
        job_type="whatsapp.accept_inbound",
        payload={"webhook_event_id": str(read_event.id)},
        attempts=1,
        max_attempts=3,
    )
    await accept_inbound_whatsapp_job(read_job)

    late_sent_event = await add_status_event(
        tenant.id,
        message.provider_message_id,
        "sent",
    )
    sent_job = ClaimedJob(
        id=uuid4(),
        tenant_id=tenant.id,
        job_type="whatsapp.accept_inbound",
        payload={"webhook_event_id": str(late_sent_event.id)},
        attempts=1,
        max_attempts=3,
    )
    await accept_inbound_whatsapp_job(sent_job)

    async with AsyncSessionFactory() as session:
        persisted_message = await session.get(WhatsAppOutboundMessage, message.id)
        persisted_read_event = await session.get(WhatsAppWebhookEvent, read_event.id)
        persisted_sent_event = await session.get(WhatsAppWebhookEvent, late_sent_event.id)

    assert persisted_message is not None
    assert persisted_message.status == "read"
    assert persisted_message.read_at is not None
    assert persisted_read_event is not None
    assert persisted_read_event.status == "processed"
    assert persisted_sent_event is not None
    assert persisted_sent_event.status == "processed"

    async with AsyncSessionFactory() as session:
        persisted_tenant = await session.get(Tenant, tenant.id)
        await session.delete(persisted_tenant)
        await session.commit()
