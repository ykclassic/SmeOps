from contextlib import nullcontext
from types import SimpleNamespace
from uuid import uuid4

from fastapi.testclient import TestClient

from app.api import telegram as telegram_api
from app.core.telegram import hash_telegram_webhook_secret
from app.db.session import get_db_session
from app.main import app


class FakeResult:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class FakeNested:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


class FakeSession:
    @property
    def no_autoflush(self):
        return nullcontext()

    def __init__(self, connection):
        self.connection = connection
        self.events = []
        self.execute_count = 0
        self.committed = False

    async def execute(self, _statement):
        self.execute_count += 1
        if self.execute_count == 1:
            return FakeResult(self.connection)
        if self.execute_count == 2:
            return FakeResult(SimpleNamespace(status="active"))
        return FakeResult(None)

    def begin_nested(self):
        return FakeNested()

    def add(self, value):
        self.events.append(value)

    async def flush(self):
        return None

    async def commit(self):
        self.committed = True


def _client_with_session(session):
    async def override_session():
        yield session

    app.dependency_overrides[get_db_session] = override_session
    return TestClient(app)


def test_telegram_webhook_rejects_invalid_secret(monkeypatch):
    tenant_id = uuid4()
    connection = SimpleNamespace(
        tenant_id=tenant_id,
        bot_id="9016622451",
        webhook_secret_hash=hash_telegram_webhook_secret("correct-secret"),
    )
    session = FakeSession(connection)
    monkeypatch.setattr(telegram_api, "encrypt_telegram_secret", lambda value: "encrypted")
    with _client_with_session(session) as client:
        response = client.post(
            "/webhooks/telegram/123456789",
            headers={"X-Telegram-Bot-Api-Secret-Token": "wrong-secret"},
            json={"update_id": 1, "message": {}},
        )
    app.dependency_overrides.clear()

    assert response.status_code == 401
    assert session.events == []
    assert not session.committed


def test_telegram_webhook_persists_and_queues_valid_update(monkeypatch):
    tenant_id = uuid4()
    connection = SimpleNamespace(
        tenant_id=tenant_id,
        bot_id="9016622451",
        webhook_secret_hash=hash_telegram_webhook_secret("correct-secret"),
    )
    session = FakeSession(connection)
    queued = []

    async def fake_enqueue(_session, **kwargs):
        queued.append(kwargs)

    monkeypatch.setattr(telegram_api, "encrypt_telegram_secret", lambda value: "encrypted")
    monkeypatch.setattr(telegram_api, "enqueue_job", fake_enqueue)
    with _client_with_session(session) as client:
        response = client.post(
            "/webhooks/telegram/123456789",
            headers={"X-Telegram-Bot-Api-Secret-Token": "correct-secret"},
            json={
                "update_id": 987,
                "message": {
                    "message_id": 44,
                    "chat": {"id": 55},
                    "from": {"id": 66},
                    "text": "Hello",
                },
            },
        )
    app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {"status": "accepted", "queued_events": 1}
    assert session.committed
    assert len(session.events) == 1
    assert session.events[0].provider_update_id == "987"
    assert queued[0]["job_type"] == "telegram.accept_inbound"
    assert queued[0]["tenant_id"] == tenant_id


def test_telegram_webhook_rejects_malformed_update_id(monkeypatch):
    connection = SimpleNamespace(
        tenant_id=uuid4(),
        bot_id="9016622451",
        webhook_secret_hash=hash_telegram_webhook_secret("correct-secret"),
    )
    session = FakeSession(connection)
    monkeypatch.setattr(telegram_api, "encrypt_telegram_secret", lambda value: "encrypted")
    with _client_with_session(session) as client:
        response = client.post(
            "/webhooks/telegram/123456789",
            headers={"X-Telegram-Bot-Api-Secret-Token": "correct-secret"},
            json={"update_id": "not-an-integer"},
        )
    app.dependency_overrides.clear()

    assert response.status_code == 400
    assert session.events == []
    assert not session.committed
