import asyncio
from contextlib import nullcontext
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import delete, text

from app.api.telegram import _lock_active_telegram_tenant
from app.db.session import AsyncSessionFactory
from app.jobs import telegram as telegram_jobs
from app.jobs.queue import ClaimedJob
from app.models import AuditEvent, Tenant, User
from app.services import conversation_engine
from app.services.tenant_lifecycle import transition_tenant_lifecycle


class Result:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class Session:
    @property
    def no_autoflush(self):
        return nullcontext()

    def __init__(self, *values):
        self.values = list(values)
        self.committed = False
        self.execute_count = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def execute(self, _statement):
        self.execute_count += 1
        value = self.values.pop(0) if self.values else None
        return Result(value)

    async def commit(self):
        self.committed = True

    async def rollback(self):
        return None


def _job(tenant_id, event_id=None):
    return ClaimedJob(
        id=uuid4(),
        tenant_id=tenant_id,
        job_type="telegram.accept_inbound",
        payload={"webhook_event_id": str(event_id or uuid4())},
        attempts=1,
        max_attempts=3,
    )



async def _wait_for_postgres_blocker(waiting_pid, blocking_pid):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 5
    while loop.time() < deadline:
        async with AsyncSessionFactory() as observer:
            blockers = await observer.scalar(
                text("SELECT pg_blocking_pids(:pid)"),
                {"pid": waiting_pid},
            )
        if blocking_pid in (blockers or []):
            return
        await asyncio.sleep(0.01)
    raise AssertionError(
        f"PostgreSQL did not report backend {waiting_pid} blocked by {blocking_pid}"
    )


@pytest.mark.asyncio
async def test_telegram_ingress_waits_for_deactivation_lock_and_rechecks_state():
    tenant_id = uuid4()
    owner_id = uuid4()
    async with AsyncSessionFactory() as setup:
        async with setup.begin():
            setup.add(
                Tenant(
                    id=tenant_id,
                    name="ADM-012 Telegram lock test",
                    slug=f"adm012-tg-lock-{uuid4().hex}",
                    status="active",
                )
            )
            await setup.flush()
            setup.add(
                User(
                    id=owner_id,
                    tenant_id=tenant_id,
                    email=f"{owner_id.hex}@example.test",
                    password_hash="test-only-hash",
                    role="owner",
                    status="active",
                )
            )

    try:
        async with AsyncSessionFactory() as lifecycle_session:
            async with lifecycle_session.begin():
                lifecycle_pid = await lifecycle_session.scalar(
                    text("SELECT pg_backend_pid()")
                )
                await transition_tenant_lifecycle(
                    lifecycle_session,
                    tenant_id=tenant_id,
                    actor_user_id=owner_id,
                    target_status="deactivated",
                )
                started = asyncio.Event()
                request_pid = None

                async def attempt_ingress():
                    nonlocal request_pid
                    async with AsyncSessionFactory() as request_session:
                        request_pid = await request_session.scalar(
                            text("SELECT pg_backend_pid()")
                        )
                        started.set()
                        return await _lock_active_telegram_tenant(
                            request_session,
                            tenant_id,
                        )

                task = asyncio.create_task(attempt_ingress())
                await asyncio.wait_for(started.wait(), timeout=2)
                assert request_pid is not None
                await _wait_for_postgres_blocker(request_pid, lifecycle_pid)
                assert not task.done()

            assert await task is False
    finally:
        async with AsyncSessionFactory() as cleanup:
            async with cleanup.begin():
                await cleanup.execute(
                    delete(AuditEvent).where(AuditEvent.tenant_id == tenant_id)
                )
                await cleanup.execute(delete(User).where(User.tenant_id == tenant_id))
                await cleanup.execute(delete(Tenant).where(Tenant.id == tenant_id))


@pytest.mark.asyncio
@pytest.mark.parametrize("tenant_status", ["deactivated", "suspended", "pending"])
async def test_queued_telegram_event_is_consumed_without_processing_for_inactive_tenant(
    monkeypatch, tenant_status,
):
    tenant_id = uuid4()
    event = SimpleNamespace(
        id=uuid4(),
        status="queued",
        processed_at=None,
        payload_encrypted="unused",
    )
    session = Session(SimpleNamespace(status=tenant_status), event)
    monkeypatch.setattr(telegram_jobs, "AsyncSessionFactory", lambda: session)

    async def unexpected_owner_link(*_args, **_kwargs):
        raise AssertionError("inactive tenant event must not process owner links")

    async def unexpected_message_persist(*_args, **_kwargs):
        raise AssertionError("inactive tenant event must not persist conversations")

    monkeypatch.setattr(telegram_jobs, "_consume_owner_link", unexpected_owner_link)
    monkeypatch.setattr(telegram_jobs, "_persist_telegram_message", unexpected_message_persist)

    await telegram_jobs.accept_inbound_telegram_job(_job(tenant_id, event.id))

    assert event.status == "processed"
    assert event.processed_at is not None
    assert session.committed
    assert session.execute_count == 2


@pytest.mark.asyncio
async def test_queued_telegram_event_continues_for_active_tenant(monkeypatch):
    tenant_id = uuid4()
    event = SimpleNamespace(
        id=uuid4(),
        status="queued",
        processed_at=None,
        payload_encrypted="encrypted-payload",
    )
    session = Session(SimpleNamespace(status="active"), event)
    calls = []
    monkeypatch.setattr(telegram_jobs, "AsyncSessionFactory", lambda: session)
    monkeypatch.setattr(
        telegram_jobs,
        "decrypt_telegram_secret",
        lambda _value: '{"message": {}}',
    )

    async def consume_owner_link(*_args, **_kwargs):
        calls.append("owner_link")
        return False

    async def persist_message(*_args, **_kwargs):
        calls.append("persist_message")
        return None

    monkeypatch.setattr(telegram_jobs, "_consume_owner_link", consume_owner_link)
    monkeypatch.setattr(telegram_jobs, "_persist_telegram_message", persist_message)

    await telegram_jobs.accept_inbound_telegram_job(_job(tenant_id, event.id))

    assert calls == ["owner_link", "persist_message"]
    assert event.status == "processed"
    assert event.processed_at is not None
    assert session.committed


@pytest.mark.asyncio
@pytest.mark.parametrize("tenant_status", ["deactivated", "suspended", "pending"])
async def test_telegram_conversation_job_skips_before_loading_conversation_for_inactive_tenant(
    monkeypatch, tenant_status,
):
    tenant_id = uuid4()
    session = Session(SimpleNamespace(status=tenant_status))
    monkeypatch.setattr("app.db.session.AsyncSessionFactory", lambda: session)
    job = ClaimedJob(
        id=uuid4(),
        tenant_id=tenant_id,
        job_type="telegram.process_conversation",
        payload={
            "conversation_id": str(uuid4()),
            "message_id": str(uuid4()),
        },
        attempts=1,
        max_attempts=3,
    )

    await conversation_engine.process_telegram_conversation_job(job)

    assert session.execute_count == 1
    assert not session.committed
