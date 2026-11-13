import hashlib
import json
from contextlib import nullcontext
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.api import telegram as telegram_api
from app.core.telegram import hash_telegram_webhook_secret
from app.db.session import get_db_session
from app.jobs import telegram as telegram_jobs
from app.jobs import telegram_owner_notifications
from app.jobs.queue import ClaimedJob
from app.main import app


class Result:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class Nested:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


class WebhookSession:
    @property
    def no_autoflush(self):
        return nullcontext()

    def __init__(self, connection, existing=None):
        self.values = [connection, SimpleNamespace(status="active"), existing]
        self.records = []
        self.committed = False

    async def execute(self, _statement):
        return Result(self.values.pop(0))

    def begin_nested(self):
        return Nested()

    def add(self, value):
        self.records.append(value)

    async def flush(self):
        return None

    async def commit(self):
        self.committed = True


class InboundSession:
    def __init__(self, values):
        self.values = list(values)
        self.records = []
        self.calls = 0

    async def execute(self, _statement):
        self.calls += 1
        return Result(self.values.pop(0))

    def begin_nested(self):
        return Nested()

    def add(self, value):
        self.records.append(value)

    async def flush(self):
        return None


class SessionContext:
    def __init__(self, session):
        self.session = session

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, *_args):
        return False


def client_for(session):
    async def override_session():
        yield session

    app.dependency_overrides[get_db_session] = override_session
    return TestClient(app)


def connection():
    return SimpleNamespace(
        tenant_id=uuid4(),
        bot_id="9016622451",
        webhook_secret_hash=hash_telegram_webhook_secret("valid-secret"),
    )


def test_duplicate_webhook_update_is_acknowledged_without_requeue(monkeypatch):
    payload = {"update_id": 10, "message": {"text": "BOOK"}}
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    existing = SimpleNamespace(
        payload_sha256=hashlib.sha256(canonical.encode()).hexdigest()
    )
    session = WebhookSession(connection(), existing=existing)
    queued = []

    async def forbidden_enqueue(*_args, **_kwargs):
        queued.append(True)

    monkeypatch.setattr(telegram_api, "enqueue_job", forbidden_enqueue)
    with client_for(session) as client:
        response = client.post(
            "/webhooks/telegram/123456789",
            headers={"X-Telegram-Bot-Api-Secret-Token": "valid-secret"},
            json=payload,
        )
    app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["queued_events"] == 0
    assert queued == []
    assert session.records == []
    assert not session.committed


def test_webhook_rejects_malformed_json_without_persisting():
    session = WebhookSession(connection())
    with client_for(session) as client:
        response = client.post(
            "/webhooks/telegram/123456789",
            headers={
                "X-Telegram-Bot-Api-Secret-Token": "valid-secret",
                "Content-Type": "application/json",
            },
            content=b'{"update_id":',
        )
    app.dependency_overrides.clear()

    assert response.status_code == 400
    assert session.records == []
    assert not session.committed


def test_webhook_rejects_payload_larger_than_one_megabyte():
    session = WebhookSession(connection())
    with client_for(session) as client:
        response = client.post(
            "/webhooks/telegram/123456789",
            headers={
                "X-Telegram-Bot-Api-Secret-Token": "valid-secret",
                "Content-Type": "application/json",
            },
            content=b" " * 1_000_001,
        )
    app.dependency_overrides.clear()

    assert response.status_code == 413
    assert session.records == []
    assert not session.committed


@pytest.mark.parametrize(
    "chat,sender",
    [
        ({"id": -100123456, "type": "group"}, {"id": 123456, "is_bot": False}),
        ({"id": 123456, "type": "private"}, {"id": 456, "is_bot": True}),
    ],
)
@pytest.mark.asyncio
async def test_inbound_group_and_bot_messages_are_ignored(chat, sender):
    session = InboundSession([])

    result = await telegram_jobs._persist_telegram_message(
        session,
        tenant_id=uuid4(),
        payload={
            "message": {
                "message_id": 1,
                "date": 1_798_000_000,
                "chat": chat,
                "from": sender,
                "text": "BOOK",
            }
        },
    )

    assert result is None
    assert session.calls == 0
    assert session.records == []


@pytest.mark.asyncio
async def test_duplicate_inbound_message_is_not_persisted(monkeypatch):
    tenant_id = uuid4()
    conversation = SimpleNamespace(
        id=uuid4(),
        tenant_id=tenant_id,
        telegram_chat_id="123456",
        telegram_user_id="123456",
        last_inbound_at=datetime.now(UTC),
    )
    existing_message = SimpleNamespace(id=uuid4())
    session = InboundSession([conversation, existing_message])
    monkeypatch.setattr(
        "app.jobs.telegram.encrypt_telegram_secret",
        lambda value: f"cipher:{value}",
    )

    result = await telegram_jobs._persist_telegram_message(
        session,
        tenant_id=tenant_id,
        payload={
            "message": {
                "message_id": 77,
                "date": 1_798_000_000,
                "chat": {"id": 123456, "type": "private"},
                "from": {"id": 123456, "is_bot": False},
                "text": "BOOK",
            }
        },
    )

    assert result is None
    assert session.records == []


@pytest.mark.asyncio
async def test_owner_notification_worker_marks_interrupted_send_unknown(monkeypatch):
    notification = SimpleNamespace(
        id=uuid4(),
        tenant_id=uuid4(),
        status="sending",
        last_error_code=None,
    )

    class NotificationSession:
        def __init__(self):
            self.commits = 0

        async def execute(self, _statement):
            return Result(notification)

        async def commit(self):
            self.commits += 1

    session = NotificationSession()
    monkeypatch.setattr(
        telegram_owner_notifications,
        "AsyncSessionFactory",
        lambda: SessionContext(session),
    )

    async def forbidden_send(**_kwargs):
        raise AssertionError("An ambiguous prior send must never be retried")

    monkeypatch.setattr(
        telegram_owner_notifications,
        "send_text_message",
        forbidden_send,
    )

    await telegram_owner_notifications.send_owner_appointment_alert_job(
        ClaimedJob(
            id=uuid4(),
            tenant_id=notification.tenant_id,
            job_type="telegram.owner_appointment_alert",
            payload={"notification_id": str(notification.id)},
            attempts=2,
            max_attempts=3,
        )
    )

    assert notification.status == "unknown"
    assert notification.last_error_code == "previous_send_outcome_unknown"
    assert session.commits == 1
