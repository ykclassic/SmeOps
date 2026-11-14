import asyncio
import hashlib
import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import select, text

from app.core.config import get_settings
from app.core.whatsapp import encrypt_whatsapp_secret, hash_whatsapp_contact
from app.db.session import AsyncSessionFactory
from app.jobs import whatsapp as whatsapp_jobs
from app.jobs.queue import ClaimedJob
from app.models import (
    BackgroundJob,
    Tenant,
    WhatsAppConversation,
    WhatsAppConversationMessage,
    WhatsAppWebhookEvent,
)
from app.services.conversation_engine import process_whatsapp_conversation_job


@pytest.fixture(autouse=True)
def whatsapp_worker_encryption_settings(monkeypatch):
    monkeypatch.setenv(
        "WHATSAPP_TOKEN_ENCRYPTION_KEY",
        "c21lb3BzLWNpLXdoYXRzYXBwLWZlcm5ldC1rZXktMzI=",
    )
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def _create_tenant_and_event(*, status: str = "active"):
    tenant_id = uuid4()
    event_id = uuid4()
    provider_message_id = f"wamid.{uuid4().hex}"
    payload = {
        "message": {
            "from": "2348012345678",
            "id": provider_message_id,
            "timestamp": "1793433600",
            "type": "text",
            "text": {"body": "BOOK"},
        }
    }
    encoded_payload = json.dumps(payload, separators=(",", ":"))
    async with AsyncSessionFactory() as session:
        session.add(
            Tenant(
                id=tenant_id,
                name="ADM-014 WhatsApp worker lifecycle test",
                slug=f"adm014-wa-{uuid4().hex[:16]}",
                status=status,
                business_timezone="Africa/Lagos",
            )
        )
        session.add(
            WhatsAppWebhookEvent(
                id=event_id,
                tenant_id=tenant_id,
                provider_event_id=f"provider-event-{uuid4().hex}",
                event_type="message.received",
                payload_encrypted=encrypt_whatsapp_secret(encoded_payload),
                payload_sha256=hashlib.sha256(encoded_payload.encode()).hexdigest(),
                status="queued",
            )
        )
        await session.commit()
    job = ClaimedJob(
        id=uuid4(),
        tenant_id=tenant_id,
        job_type="whatsapp.accept_inbound",
        payload={"webhook_event_id": str(event_id)},
        attempts=1,
        max_attempts=3,
    )
    return tenant_id, event_id, job, provider_message_id


async def _wait_until_blocked_by(blocking_pid: int) -> None:
    deadline = asyncio.get_running_loop().time() + 5
    while asyncio.get_running_loop().time() < deadline:
        async with AsyncSessionFactory() as observer:
            blocked = await observer.scalar(
                text(
                    "SELECT EXISTS ("
                    "SELECT 1 FROM pg_stat_activity "
                    "WHERE :blocking_pid = ANY(pg_blocking_pids(pid))"
                    ")"
                ),
                {"blocking_pid": blocking_pid},
            )
        if blocked:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(
        f"No PostgreSQL backend was observed waiting on backend {blocking_pid}"
    )


async def _wait_until_pid_is_blocked(waiting_pid: int) -> None:
    deadline = asyncio.get_running_loop().time() + 5
    while asyncio.get_running_loop().time() < deadline:
        async with AsyncSessionFactory() as observer:
            blocked = await observer.scalar(
                text("SELECT cardinality(pg_blocking_pids(:pid)) > 0"),
                {"pid": waiting_pid},
            )
        if blocked:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(
        f"PostgreSQL did not report backend {waiting_pid} waiting on a row lock"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("tenant_status", ["deactivated", "suspended", "pending"])
async def test_queued_whatsapp_event_is_terminalized_for_inactive_tenant(tenant_status):
    tenant_id, event_id, job, _ = await _create_tenant_and_event(status=tenant_status)

    await whatsapp_jobs.accept_inbound_whatsapp_job(job)

    async with AsyncSessionFactory() as session:
        event = await session.get(WhatsAppWebhookEvent, event_id)
        conversations = await session.execute(
            select(WhatsAppConversation).where(
                WhatsAppConversation.tenant_id == tenant_id
            )
        )
        messages = await session.execute(
            select(WhatsAppConversationMessage).where(
                WhatsAppConversationMessage.tenant_id == tenant_id
            )
        )
        downstream_jobs = await session.execute(
            select(BackgroundJob).where(
                BackgroundJob.tenant_id == tenant_id,
                BackgroundJob.job_type == "whatsapp.process_conversation",
            )
        )

    assert event is not None
    assert event.status == "processed"
    assert event.processed_at is not None
    assert conversations.scalars().all() == []
    assert messages.scalars().all() == []
    assert downstream_jobs.scalars().all() == []


@pytest.mark.asyncio
async def test_whatsapp_accept_worker_waits_for_lifecycle_lock_and_discards_after_deactivation():
    tenant_id, event_id, job, _ = await _create_tenant_and_event(status="active")

    async with AsyncSessionFactory() as lifecycle_session:
        await lifecycle_session.execute(
            select(Tenant).where(Tenant.id == tenant_id).with_for_update()
        )
        lifecycle_pid = await lifecycle_session.scalar(text("SELECT pg_backend_pid()"))

        worker_task = asyncio.create_task(
            whatsapp_jobs.accept_inbound_whatsapp_job(job)
        )
        await _wait_until_blocked_by(lifecycle_pid)

        tenant = await lifecycle_session.get(Tenant, tenant_id)
        assert tenant is not None
        tenant.status = "deactivated"
        await lifecycle_session.commit()
        await asyncio.wait_for(worker_task, timeout=5)

    async with AsyncSessionFactory() as session:
        event = await session.get(WhatsAppWebhookEvent, event_id)
        conversations = await session.execute(
            select(WhatsAppConversation).where(
                WhatsAppConversation.tenant_id == tenant_id
            )
        )
        messages = await session.execute(
            select(WhatsAppConversationMessage).where(
                WhatsAppConversationMessage.tenant_id == tenant_id
            )
        )
        downstream_jobs = await session.execute(
            select(BackgroundJob).where(
                BackgroundJob.tenant_id == tenant_id,
                BackgroundJob.job_type == "whatsapp.process_conversation",
            )
        )

    assert event is not None and event.status == "processed"
    assert conversations.scalars().all() == []
    assert messages.scalars().all() == []
    assert downstream_jobs.scalars().all() == []


@pytest.mark.asyncio
async def test_running_whatsapp_accept_worker_serializes_real_writes_before_deactivation(
    monkeypatch,
):
    tenant_id, event_id, job, provider_message_id = await _create_tenant_and_event(
        status="active"
    )
    entered_enqueue = asyncio.Event()
    release_enqueue = asyncio.Event()
    original_enqueue_job = whatsapp_jobs.enqueue_job

    async def pause_after_real_enqueue(*args, **kwargs):
        result = await original_enqueue_job(*args, **kwargs)
        entered_enqueue.set()
        await release_enqueue.wait()
        return result

    # Let the real inbound processor create its conversation/message and the
    # real queue helper persist the downstream job. Pause before the worker's
    # commit so a concurrent lifecycle transaction must wait on the tenant row.
    monkeypatch.setattr(whatsapp_jobs, "enqueue_job", pause_after_real_enqueue)
    worker_task = asyncio.create_task(
        whatsapp_jobs.accept_inbound_whatsapp_job(job)
    )
    await asyncio.wait_for(entered_enqueue.wait(), timeout=5)

    async with AsyncSessionFactory() as lifecycle_session:
        lifecycle_pid = await lifecycle_session.scalar(text("SELECT pg_backend_pid()"))
        lifecycle_task = asyncio.create_task(
            lifecycle_session.execute(
                select(Tenant).where(Tenant.id == tenant_id).with_for_update()
            )
        )
        await _wait_until_pid_is_blocked(lifecycle_pid)

        release_enqueue.set()
        await asyncio.wait_for(worker_task, timeout=5)
        await asyncio.wait_for(lifecycle_task, timeout=5)
        tenant = await lifecycle_session.get(Tenant, tenant_id)
        assert tenant is not None
        tenant.status = "deactivated"
        await lifecycle_session.commit()

    async with AsyncSessionFactory() as session:
        event = await session.get(WhatsAppWebhookEvent, event_id)
        tenant = await session.get(Tenant, tenant_id)
        conversations = await session.execute(
            select(WhatsAppConversation).where(
                WhatsAppConversation.tenant_id == tenant_id
            )
        )
        messages = await session.execute(
            select(WhatsAppConversationMessage).where(
                WhatsAppConversationMessage.tenant_id == tenant_id,
                WhatsAppConversationMessage.provider_message_id == provider_message_id,
            )
        )
        downstream_jobs = await session.execute(
            select(BackgroundJob).where(
                BackgroundJob.tenant_id == tenant_id,
                BackgroundJob.job_type == "whatsapp.process_conversation",
            )
        )

    conversation_rows = conversations.scalars().all()
    message_rows = messages.scalars().all()
    job_rows = downstream_jobs.scalars().all()
    assert event is not None and event.status == "processed"
    assert event.processed_at is not None
    assert tenant is not None and tenant.status == "deactivated"
    assert len(conversation_rows) == 1
    assert len(message_rows) == 1
    assert len(job_rows) == 1
    assert job_rows[0].payload["message_id"] == str(message_rows[0].id)
    assert job_rows[0].payload["conversation_id"] == str(conversation_rows[0].id)


@pytest.mark.asyncio
@pytest.mark.parametrize("tenant_status", ["deactivated", "suspended"])
async def test_whatsapp_conversation_worker_skips_inactive_tenant(tenant_status):
    tenant_id = uuid4()
    conversation_id = uuid4()
    message_id = uuid4()
    now = datetime.now(UTC)
    phone = "2348012345678"

    async with AsyncSessionFactory() as session:
        session.add(
            Tenant(
                id=tenant_id,
                name="ADM-014 inactive conversation test",
                slug=f"adm014-conv-{uuid4().hex[:16]}",
                status=tenant_status,
                business_timezone="Africa/Lagos",
            )
        )
        session.add(
            WhatsAppConversation(
                id=conversation_id,
                tenant_id=tenant_id,
                customer_phone_hash=hash_whatsapp_contact(phone),
                customer_phone_encrypted=encrypt_whatsapp_secret(phone),
                status="active",
                context={"booking": {"step": "awaiting_confirmation"}},
                last_inbound_at=now,
            )
        )
        session.add(
            WhatsAppConversationMessage(
                id=message_id,
                tenant_id=tenant_id,
                conversation_id=conversation_id,
                provider_message_id=f"wamid.{uuid4().hex}",
                direction="inbound",
                message_type="text",
                body_encrypted=encrypt_whatsapp_secret("YES"),
                provider_timestamp=now,
                delivery_status="received",
            )
        )
        await session.commit()

    job = ClaimedJob(
        id=uuid4(),
        tenant_id=tenant_id,
        job_type="whatsapp.process_conversation",
        payload={
            "conversation_id": str(conversation_id),
            "message_id": str(message_id),
        },
        attempts=1,
        max_attempts=3,
    )
    await process_whatsapp_conversation_job(job)

    async with AsyncSessionFactory() as session:
        conversation = await session.get(WhatsAppConversation, conversation_id)
        outbound_jobs = await session.execute(
            select(BackgroundJob).where(
                BackgroundJob.tenant_id == tenant_id,
                BackgroundJob.job_type.like("whatsapp.%"),
            )
        )

    assert conversation is not None
    assert conversation.context == {"booking": {"step": "awaiting_confirmation"}}
    assert outbound_jobs.scalars().all() == []
