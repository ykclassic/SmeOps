from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from app.core.config import get_settings
from app.core.whatsapp import (
    decrypt_whatsapp_secret,
    encrypt_whatsapp_secret,
    hash_whatsapp_contact,
)
from app.db.session import AsyncSessionFactory
from app.jobs.queue import ClaimedJob
from app.jobs.whatsapp_outbound import send_outbound_whatsapp_job
from app.models import (
    BackgroundJob,
    Tenant,
    WhatsAppConnection,
    WhatsAppConversation,
    WhatsAppOutboundMessage,
)
from app.services.whatsapp_outbound import (
    OutboundIdempotencyConflict,
    OutboundPolicyViolation,
    enqueue_whatsapp_text,
)


@pytest.fixture(autouse=True)
def whatsapp_settings(monkeypatch):
    monkeypatch.setenv(
        "WHATSAPP_TOKEN_ENCRYPTION_KEY",
        "c21lb3BzLWNpLXdoYXRzYXBwLWZlcm5ldC1rZXktMzI=",
    )
    monkeypatch.setenv("WHATSAPP_GRAPH_API_VERSION", "v25.0")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def create_connection() -> tuple[Tenant, UUID]:
    tenant = Tenant(
        id=uuid4(),
        name="Outbound Test Business",
        slug=f"outbound-{uuid4().hex[:12]}",
        business_timezone="Africa/Lagos",
        status="active",
    )
    customer_phone = "2348012345678"
    connection = WhatsAppConnection(
        tenant_id=tenant.id,
        phone_number_id=f"{uuid4().int % 10**11:011d}",
        business_account_id=f"{uuid4().int % 10**11:011d}",
        access_token_encrypted=encrypt_whatsapp_secret("test-meta-access-token"),
        status="active",
    )
    conversation = WhatsAppConversation(
        tenant_id=tenant.id,
        customer_phone_hash=hash_whatsapp_contact(customer_phone),
        customer_phone_encrypted=encrypt_whatsapp_secret(customer_phone),
        status="active",
        context={},
        last_inbound_at=datetime.now(UTC),
    )
    async with AsyncSessionFactory() as session:
        session.add_all([tenant, connection, conversation])
        await session.commit()
    return tenant, connection.id


@pytest.mark.asyncio
async def test_outbound_message_is_encrypted_and_idempotent():
    tenant, _connection_id = await create_connection()
    key = f"test:{uuid4()}"

    async with AsyncSessionFactory() as session:
        first = await enqueue_whatsapp_text(
            session,
            tenant_id=tenant.id,
            recipient="+2348012345678",
            body="Your appointment is confirmed.",
            idempotency_key=key,
        )
        first_id = first.id

    async with AsyncSessionFactory() as session:
        replay = await enqueue_whatsapp_text(
            session,
            tenant_id=tenant.id,
            recipient="2348012345678",
            body="Your appointment is confirmed.",
            idempotency_key=key,
        )
        assert replay.id == first_id
        with pytest.raises(OutboundIdempotencyConflict):
            await enqueue_whatsapp_text(
                session,
                tenant_id=tenant.id,
                recipient="2348012345678",
                body="A different message",
                idempotency_key=key,
            )

    async with AsyncSessionFactory() as session:
        message = await session.get(WhatsAppOutboundMessage, first_id)
        job_result = await session.execute(
            select(BackgroundJob).where(
                BackgroundJob.tenant_id == tenant.id,
                BackgroundJob.job_type == "whatsapp.send_text",
            )
        )
        job = job_result.scalar_one()

    assert message is not None
    assert message.status == "queued"
    assert message.recipient_encrypted != "2348012345678"
    assert message.body_encrypted != "Your appointment is confirmed."
    assert decrypt_whatsapp_secret(message.recipient_encrypted) == "2348012345678"
    assert decrypt_whatsapp_secret(message.body_encrypted) == "Your appointment is confirmed."
    assert job.max_attempts == 2
    assert job.status == "queued"

    async with AsyncSessionFactory() as session:
        persisted_tenant = await session.get(Tenant, tenant.id)
        await session.delete(persisted_tenant)
        await session.commit()


@pytest.mark.asyncio
async def test_unknown_provider_outcome_is_not_retried(monkeypatch):
    tenant, _connection_id = await create_connection()
    key = f"test:{uuid4()}"
    async with AsyncSessionFactory() as session:
        message = await enqueue_whatsapp_text(
            session,
            tenant_id=tenant.id,
            recipient="2348012345678",
            body="One-time send",
            idempotency_key=key,
        )
        message_id = message.id

    async def unknown_outcome(**_kwargs):
        from app.integrations.whatsapp_cloud import WhatsAppDeliveryUnknown

        raise WhatsAppDeliveryUnknown("network outcome unknown")

    monkeypatch.setattr(
        "app.jobs.whatsapp_outbound.send_text_message",
        unknown_outcome,
    )
    job = ClaimedJob(
        id=uuid4(),
        tenant_id=tenant.id,
        job_type="whatsapp.send_text",
        payload={"message_id": str(message_id)},
        attempts=1,
        max_attempts=2,
    )
    await send_outbound_whatsapp_job(job)

    async with AsyncSessionFactory() as session:
        persisted = await session.get(WhatsAppOutboundMessage, message_id)
    assert persisted is not None
    assert persisted.status == "unknown"
    assert persisted.last_error_code == "provider_outcome_unknown"

    async with AsyncSessionFactory() as session:
        persisted_tenant = await session.get(Tenant, tenant.id)
        await session.delete(persisted_tenant)
        await session.commit()


@pytest.mark.asyncio
async def test_recovery_of_stale_sending_message_never_calls_provider(monkeypatch):
    tenant, _connection_id = await create_connection()
    async with AsyncSessionFactory() as session:
        message = await enqueue_whatsapp_text(
            session,
            tenant_id=tenant.id,
            recipient="2348012345678",
            body="Potentially already sent",
            idempotency_key=f"test:{uuid4()}",
        )
        message_id = message.id
        message.status = "sending"
        await session.commit()

    async def must_not_send(**_kwargs):
        raise AssertionError("stale sending message must not be sent again")

    monkeypatch.setattr(
        "app.jobs.whatsapp_outbound.send_text_message",
        must_not_send,
    )
    job = ClaimedJob(
        id=uuid4(),
        tenant_id=tenant.id,
        job_type="whatsapp.send_text",
        payload={"message_id": str(message_id)},
        attempts=2,
        max_attempts=2,
    )
    await send_outbound_whatsapp_job(job)

    async with AsyncSessionFactory() as session:
        persisted = await session.get(WhatsAppOutboundMessage, message_id)
    assert persisted is not None
    assert persisted.status == "unknown"
    assert persisted.last_error_code == "previous_send_outcome_unknown"

    async with AsyncSessionFactory() as session:
        persisted_tenant = await session.get(Tenant, tenant.id)
        await session.delete(persisted_tenant)
        await session.commit()


@pytest.mark.asyncio
async def test_expired_customer_service_window_blocks_enqueue():
    tenant, _connection_id = await create_connection()
    async with AsyncSessionFactory() as session:
        result = await session.execute(
            select(WhatsAppConversation).where(
                WhatsAppConversation.tenant_id == tenant.id
            )
        )
        conversation = result.scalar_one()
        conversation.last_inbound_at = datetime.now(UTC) - timedelta(hours=24)
        await session.commit()

    async with AsyncSessionFactory() as session:
        with pytest.raises(OutboundPolicyViolation) as exc_info:
            await enqueue_whatsapp_text(
                session,
                tenant_id=tenant.id,
                recipient="2348012345678",
                body="Outside the service window",
                idempotency_key=f"test:{uuid4()}",
            )
        assert exc_info.value.error_code == "customer_service_window_closed"

    async with AsyncSessionFactory() as session:
        persisted_tenant = await session.get(Tenant, tenant.id)
        await session.delete(persisted_tenant)
        await session.commit()


@pytest.mark.asyncio
async def test_opted_out_customer_cannot_be_messaged():
    tenant, _connection_id = await create_connection()
    async with AsyncSessionFactory() as session:
        result = await session.execute(
            select(WhatsAppConversation).where(
                WhatsAppConversation.tenant_id == tenant.id
            )
        )
        conversation = result.scalar_one()
        conversation.is_opted_out = True
        conversation.opted_out_at = datetime.now(UTC)
        await session.commit()

    async with AsyncSessionFactory() as session:
        with pytest.raises(OutboundPolicyViolation) as exc_info:
            await enqueue_whatsapp_text(
                session,
                tenant_id=tenant.id,
                recipient="2348012345678",
                body="Customer opted out",
                idempotency_key=f"test:{uuid4()}",
            )
        assert exc_info.value.error_code == "customer_opted_out"

    async with AsyncSessionFactory() as session:
        persisted_tenant = await session.get(Tenant, tenant.id)
        await session.delete(persisted_tenant)
        await session.commit()


@pytest.mark.asyncio
async def test_worker_rechecks_service_window_before_provider_call(monkeypatch):
    tenant, _connection_id = await create_connection()
    async with AsyncSessionFactory() as session:
        message = await enqueue_whatsapp_text(
            session,
            tenant_id=tenant.id,
            recipient="2348012345678",
            body="Window may expire while queued",
            idempotency_key=f"test:{uuid4()}",
        )
        message_id = message.id

    async with AsyncSessionFactory() as session:
        result = await session.execute(
            select(WhatsAppConversation).where(
                WhatsAppConversation.tenant_id == tenant.id
            )
        )
        conversation = result.scalar_one()
        conversation.last_inbound_at = datetime.now(UTC) - timedelta(hours=25)
        await session.commit()

    async def must_not_send(**_kwargs):
        raise AssertionError("expired customer-service window must block provider send")

    monkeypatch.setattr(
        "app.jobs.whatsapp_outbound.send_text_message",
        must_not_send,
    )
    job = ClaimedJob(
        id=uuid4(),
        tenant_id=tenant.id,
        job_type="whatsapp.send_text",
        payload={"message_id": str(message_id)},
        attempts=1,
        max_attempts=2,
    )
    await send_outbound_whatsapp_job(job)

    async with AsyncSessionFactory() as session:
        persisted = await session.get(WhatsAppOutboundMessage, message_id)
    assert persisted is not None
    assert persisted.status == "failed"
    assert persisted.last_error_code == "customer_service_window_closed"

    async with AsyncSessionFactory() as session:
        persisted_tenant = await session.get(Tenant, tenant.id)
        await session.delete(persisted_tenant)
        await session.commit()
