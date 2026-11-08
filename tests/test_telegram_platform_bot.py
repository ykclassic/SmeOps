import hashlib
from types import SimpleNamespace
from uuid import uuid4

from fastapi.testclient import TestClient

from app.api import telegram as telegram_api
from app.core.telegram import hash_telegram_webhook_secret
from app.db.session import get_db_session
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


class Session:
    def __init__(self, values):
        self.values = list(values)
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


def client_for(session):
    async def override_session():
        yield session

    app.dependency_overrides[get_db_session] = override_session
    return TestClient(app)


def platform_bot():
    return SimpleNamespace(
        id=uuid4(),
        bot_id="901662451",
        bot_username="smeops_bot",
        status="active",
        webhook_secret_hash=hash_telegram_webhook_secret("platform-secret"),
    )


def start_update(token, update_id=100):
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "chat": {"id": 7001, "type": "private"},
            "from": {"id": 7001, "is_bot": False},
            "text": f"/start {token}",
        },
    }


def test_shared_bot_resolves_booking_link_and_queues_tenant_scoped_event(monkeypatch):
    tenant_id = uuid4()
    bot = platform_bot()
    reference = SimpleNamespace(tenant_id=tenant_id)
    session = Session([bot, None, None, reference, None])
    jobs = []

    async def enqueue(_session, **kwargs):
        jobs.append(kwargs)

    monkeypatch.setattr(telegram_api, "enqueue_job", enqueue)
    monkeypatch.setattr(telegram_api, "encrypt_telegram_secret", lambda value: f"enc:{value}")
    with client_for(session) as client:
        response = client.post(
            "/webhooks/telegram/platform",
            headers={"X-Telegram-Bot-Api-Secret-Token": "platform-secret"},
            json=start_update("valid-booking-reference"),
        )
    app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {"status": "accepted", "queued_events": 1}
    assert session.committed
    assert len(session.records) == 2  # selected chat context and webhook event
    event = next(record for record in session.records if hasattr(record, "provider_update_id"))
    assert event.tenant_id == tenant_id
    assert event.bot_id == "901662451"
    assert jobs[0]["tenant_id"] == tenant_id
    assert jobs[0]["idempotency_key"] == "tg:platform:901662451:100"


def test_invalid_booking_reference_never_falls_back_to_existing_chat_context():
    bot = platform_bot()
    session = Session([bot, None, None, None])
    with client_for(session) as client:
        response = client.post(
            "/webhooks/telegram/platform",
            headers={"X-Telegram-Bot-Api-Secret-Token": "platform-secret"},
            json=start_update("invalid-reference"),
        )
    app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["queued_events"] == 0
    assert session.records == []
    assert not session.committed


def test_replayed_start_update_is_deduplicated_before_switching_tenant_context():
    bot = platform_bot()
    duplicate = SimpleNamespace(payload_sha256="a" * 64)
    session = Session([bot, duplicate])
    with client_for(session) as client:
        response = client.post(
            "/webhooks/telegram/platform",
            headers={"X-Telegram-Bot-Api-Secret-Token": "platform-secret"},
            json=start_update("old-business-reference"),
        )
    app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["queued_events"] == 0
    assert session.records == []
    assert not session.committed
