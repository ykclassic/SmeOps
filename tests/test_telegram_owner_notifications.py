import hashlib
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.jobs import telegram as telegram_jobs
from app.services import telegram_owner_notifications


class Result:
    def __init__(self, value):
        self.value = value

    def scalars(self):
        return self

    def all(self):
        return self.value

    def scalar_one_or_none(self):
        return self.value


class Session:
    def __init__(self, results):
        self.results = list(results)
        self.records = []
        self.commits = 0

    async def execute(self, _statement):
        return Result(self.results.pop(0))

    def add(self, value):
        self.records.append(value)

    async def flush(self):
        return None

    async def commit(self):
        self.commits += 1


@pytest.mark.asyncio
async def test_valid_owner_start_token_is_consumed_and_audited():
    token = "short-lived-random-token"
    owner_link = SimpleNamespace(
        id=uuid4(),
        owner_user_id=uuid4(),
        invitation_hash=hashlib.sha256(token.encode()).hexdigest(),
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        linked_at=None,
        disabled_at=None,
        chat_id=None,
    )
    session = Session([owner_link])

    consumed = await telegram_jobs._consume_owner_link(
        session,
        tenant_id=uuid4(),
        payload={
            "message": {
                "text": f"/start {token}",
                "chat": {"id": 998877, "type": "private"},
                "from": {"id": 998877, "is_bot": False},
            }
        },
    )

    assert consumed is True
    assert owner_link.chat_id == "998877"
    assert owner_link.linked_at is not None
    assert len(session.records) == 1
    assert session.records[0].event_type == "telegram.owner_link.connected"


@pytest.mark.asyncio
async def test_expired_or_unknown_owner_start_token_is_not_linked():
    session = Session([None])

    consumed = await telegram_jobs._consume_owner_link(
        session,
        tenant_id=uuid4(),
        payload={
            "message": {
                "text": "/start invalid-token",
                "chat": {"id": 998877, "type": "private"},
                "from": {"id": 998877, "is_bot": False},
            }
        },
    )

    assert consumed is True
    assert session.records == []


@pytest.mark.asyncio
async def test_owner_link_commands_in_groups_are_not_consumed_as_links():
    session = Session([])

    consumed = await telegram_jobs._consume_owner_link(
        session,
        tenant_id=uuid4(),
        payload={
            "message": {
                "text": "/start token",
                "chat": {"id": -100998877, "type": "group"},
                "from": {"id": 998877, "is_bot": False},
            }
        },
    )

    assert consumed is True
    assert session.records == []


@pytest.mark.asyncio
async def test_appointment_alert_is_queued_once_per_linked_owner(monkeypatch):
    tenant_id = uuid4()
    owner_id = uuid4()
    appointment_id = uuid4()
    link = SimpleNamespace(
        tenant_id=tenant_id,
        owner_user_id=owner_id,
        chat_id="998877",
        linked_at=datetime.now(UTC),
        disabled_at=None,
    )
    session = Session([[link], None])
    jobs = []

    async def enqueue(_session, **kwargs):
        jobs.append(kwargs)

    monkeypatch.setattr(telegram_owner_notifications, "enqueue_job", enqueue)

    count = await telegram_owner_notifications.enqueue_appointment_owner_alerts(
        session,
        tenant_id=tenant_id,
        appointment_id=appointment_id,
    )

    assert count == 1
    assert len(session.records) == 1
    assert jobs[0]["job_type"] == "telegram.owner_appointment_alert"
    assert jobs[0]["tenant_id"] == tenant_id
    assert jobs[0]["idempotency_key"] == (
        f"tg:owner-alert:{appointment_id}:{owner_id}"
    )


@pytest.mark.asyncio
async def test_existing_owner_notification_is_not_queued_again(monkeypatch):
    tenant_id = uuid4()
    owner_id = uuid4()
    appointment_id = uuid4()
    link = SimpleNamespace(
        tenant_id=tenant_id,
        owner_user_id=owner_id,
        chat_id="998877",
        linked_at=datetime.now(UTC),
        disabled_at=None,
    )
    existing = SimpleNamespace(id=uuid4())
    session = Session([[link], existing])
    jobs = []

    async def enqueue(_session, **kwargs):
        jobs.append(kwargs)

    monkeypatch.setattr(telegram_owner_notifications, "enqueue_job", enqueue)

    count = await telegram_owner_notifications.enqueue_appointment_owner_alerts(
        session,
        tenant_id=tenant_id,
        appointment_id=appointment_id,
    )

    assert count == 0
    assert jobs == []
    assert session.records == []
