from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.api import telegram as telegram_api
from app.core.telegram import hash_telegram_webhook_secret
from app.db.session import get_db_session
from app.jobs.telegram import _persist_telegram_message
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
    def __init__(self, connection, existing=None):
        self.connection = connection
        self.existing = existing
        self.events = []
        self.committed = False
        self.calls = 0

    async def execute(self, _statement):
        self.calls += 1
        return Result(self.connection if self.calls == 1 else self.existing)

    def begin_nested(self):
        return Nested()

    def add(self, value):
        self.events.append(value)

    async def flush(self):
        return None

    async def commit(self):
        self.committed = True


class InboundSession:
    def __init__(self, existing_conversation=None, existing_message=None):
        self.existing_conversation = existing_conversation
        self.existing_message = existing_message
        self.calls = 0
        self.records = []

    async def execute(self, _statement):
        self.calls += 1
        if self.calls == 1:
            return Result(self.existing_conversation)
        return Result(self.existing_message)

    def begin_nested(self):
        return Nested()

    def add(self, value):
        self.records.append(value)

    async def flush(self):
        return None


def client_for(session):
    async def override_session():
        yield session

    app.dependency_overrides[get_db_session] = override_session
    return TestClient(app)


def telegram_connection():
    return SimpleNamespace(
        tenant_id=uuid4(),
        webhook_secret_hash=hash_telegram_webhook_secret("valid-webhook-secret"),
    )


def test_duplicate_webhook_update_is_acknowledged_without_requeue(monkeypatch):
    connection = telegram_connection()
    existing = SimpleNamespace(payload_sha256="a" * 64)
    session = WebhookSession(connection, existing=existing)
    queued = []

    async def forbidden_enqueue(*_args, **_kwargs):
        queued.append(True)

    monkeypatch.setattr(telegram_api, "enqueue_job", forbidden_enqueue)
    with client_for(session) as client:
        response = client.post(
            "/webhooks/telegram/123456789",
            headers={"X-Telegram-Bot-Api-Secret-Token": "valid-webhook-secret"},
            json={"update_id": 991, "message": {"text": "BOOK"}},
        )
    app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {"status": "accepted", "queued_events": 0}
    assert queued == []
    assert session.events == []
    assert not session.committed


def test_webhook_rejects_body_over_one_megabyte(monkeypatch):
    session = WebhookSession(telegram_connection())
    with client_for(session) as client:
        response = client.post(
            "/webhooks/telegram/123456789",
            headers={
                "X-Telegram-Bot-Api-Secret-Token": "valid-webhook-secret",
                "Content-Type": "application/json",
            },
            content=b" " * 1_000_001,
        )
    app.dependency_overrides.clear()

    assert response.status_code == 413
    assert session.events == []
    assert not session.committed


def test_webhook_rejects_malformed_json_without_persisting(monkeypatch):
    session = WebhookSession(telegram_connection())
    with client_for(session) as client:
        response = client.post(
            "/webhooks/telegram/123456789",
            headers={"X-Telegram-Bot-Api-Secret-Token": "valid-webhook-secret"},
            content=b'{"update_id":',
        )
    app.dependency_overrides.clear()

    assert response.status_code == 400
    assert session.events == []
    assert not session.committed


@pytest.mark.parametrize(
    "chat,sender",
    [
        ({"id": -100123456789, "type": "group"}, {"id": 456, "is_bot": False}),
        ({"id": 123456, "type": "private"}, {"id": 456, "is_bot": True}),
    ],
)
@pytest.mark.asyncio
async def test_inbound_persistence_ignores_group_chats_and_bot_authors(
    chat, sender, monkeypatch
):
    session = InboundSession()
    payload = {
        "message": {
            "message_id": 1,
            "date": 1_798_000_000,
            "chat": chat,
            "from": sender,
            "text": "BOOK",
        }
    }

    result = await _persist_telegram_message(
        session,
        tenant_id=uuid4(),
        payload=payload,
    )

    assert result is None
    assert session.records == []
    assert session.calls == 0


@pytest.mark.asyncio
async def test_duplicate_inbound_message_is_not_persisted(monkeypatch):
    conversation = SimpleNamespace(
        id=uuid4(),
        tenant_id=uuid4(),
        telegram_chat_id="123456",
        telegram_user_id="456",
        last_inbound_at=None,
    )
    existing = SimpleNamespace(id=uuid4())
    session = InboundSession(
        existing_conversation=conversation,
        existing_message=existing,
    )
    monkeypatch.setattr(
        "app.jobs.telegram.encrypt_telegram_secret",
        lambda value: f"cipher:{value}",
    )

    result = await _persist_telegram_message(
        session,
        tenant_id=conversation.tenant_id,
        payload={
            "message": {
                "message_id": 77,
                "date": 1_798_000_000,
                "chat": {"id": 123456, "type": "private"},
                "from": {"id": 456, "is_bot": False},
                "text": "BOOK",
            }
        },
    )

    assert result is None
    assert session.records == []
