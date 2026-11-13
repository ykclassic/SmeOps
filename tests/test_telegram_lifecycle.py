from contextlib import nullcontext
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.jobs import telegram as telegram_jobs
from app.jobs.queue import ClaimedJob
from app.services import conversation_engine


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


@pytest.mark.asyncio
async def test_queued_telegram_event_is_consumed_without_processing_for_inactive_tenant(
    monkeypatch,
):
    tenant_id = uuid4()
    event = SimpleNamespace(
        id=uuid4(),
        status="queued",
        processed_at=None,
        payload_encrypted="unused",
    )
    session = Session(SimpleNamespace(status="deactivated"), event)
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
async def test_telegram_conversation_job_skips_before_loading_conversation_for_inactive_tenant(
    monkeypatch,
):
    tenant_id = uuid4()
    session = Session(SimpleNamespace(status="suspended"))
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
