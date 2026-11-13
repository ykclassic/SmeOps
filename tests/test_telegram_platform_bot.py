from contextlib import nullcontext
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
    @property
    def no_autoflush(self):
        return nullcontext()

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
        bot_id="9016622451",
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
    session = Session([bot, None, None, reference, None, SimpleNamespace(status="active")])
    jobs = []

    async def enqueue(_session, **kwargs):
        jobs.append(kwargs)

    monkeypatch.setattr(telegram_api, "enqueue_job", enqueue)
    monkeypatch.setattr(telegram_api, "encrypt_telegram_secret", lambda value: f"enc:{value}")
    with client_for(session) as client:
        response = client.post(
            "/webhooks/telegram/platform",
            headers={"X-Telegram-Bot-Api-Secret-Token": "platform-secret"},
            json=start_update("book_valid-booking-reference"),
        )
    app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {"status": "accepted", "queued_events": 1}
    assert session.committed
    assert len(session.records) == 2  # selected chat context and webhook event
    event = next(record for record in session.records if hasattr(record, "provider_update_id"))
    assert event.tenant_id == tenant_id
    assert event.bot_id == "9016622451"
    assert jobs[0]["tenant_id"] == tenant_id
    assert jobs[0]["idempotency_key"] == "tg:platform:9016622451:100"


def test_invalid_booking_reference_never_falls_back_to_existing_chat_context():
    bot = platform_bot()
    session = Session([bot, None, None, None])
    with client_for(session) as client:
        response = client.post(
            "/webhooks/telegram/platform",
            headers={"X-Telegram-Bot-Api-Secret-Token": "platform-secret"},
            json=start_update("book_invalid-reference"),
        )
    app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["queued_events"] == 0
    assert session.records == []
    assert not session.committed


def test_inactive_tenant_booking_link_is_ignored_without_switching_chat_context():
    tenant_id = uuid4()
    bot = platform_bot()
    reference = SimpleNamespace(tenant_id=tenant_id)
    inactive_tenant = SimpleNamespace(status="deactivated")
    session = Session([bot, None, None, reference, None, inactive_tenant])
    jobs = []

    async def enqueue(_session, **kwargs):
        jobs.append(kwargs)

    with client_for(session) as client:
        response = client.post(
            "/webhooks/telegram/platform",
            headers={"X-Telegram-Bot-Api-Secret-Token": "platform-secret"},
            json=start_update("book_deactivated-business-reference"),
        )
    app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {"status": "ignored", "queued_events": 0}
    assert session.records == []
    assert not session.committed
    assert jobs == []


def test_inactive_existing_chat_context_cannot_continue_booking():
    bot = platform_bot()
    tenant_id = uuid4()
    context = SimpleNamespace(tenant_id=tenant_id)
    session = Session([bot, None, context, SimpleNamespace(status="suspended")])
    payload = {
        "update_id": 301,
        "message": {
            "message_id": 301,
            "chat": {"id": 7001, "type": "private"},
            "from": {"id": 7001, "is_bot": False},
            "text": "BOOK",
        },
    }
    with client_for(session) as client:
        response = client.post(
            "/webhooks/telegram/platform",
            headers={"X-Telegram-Bot-Api-Secret-Token": "platform-secret"},
            json=payload,
        )
    app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {"status": "ignored", "queued_events": 0}
    assert session.records == []
    assert not session.committed


def test_replayed_start_update_is_deduplicated_before_switching_tenant_context():
    bot = platform_bot()
    duplicate = SimpleNamespace(id=uuid4(), payload_sha256="a" * 64)
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


def test_same_platform_bot_switches_between_two_businesses_only_on_valid_links(monkeypatch):
    bot = platform_bot()
    tenant_a = uuid4()
    tenant_b = uuid4()
    context = None
    resolved_tenants = []
    jobs = []

    async def enqueue(_session, **kwargs):
        jobs.append(kwargs)

    monkeypatch.setattr(telegram_api, "enqueue_job", enqueue)
    monkeypatch.setattr(telegram_api, "encrypt_telegram_secret", lambda value: f"enc:{value}")

    for update_id, tenant_id, token in (
        (201, tenant_a, "book_tenant-a-reference"),
        (202, tenant_b, "book_tenant-b-reference"),
    ):
        reference = SimpleNamespace(tenant_id=tenant_id)
        session = Session([bot, None, None, reference, context, SimpleNamespace(status="active")])
        payload = start_update(token, update_id=update_id)
        with client_for(session) as client:
            response = client.post(
                "/webhooks/telegram/platform",
                headers={"X-Telegram-Bot-Api-Secret-Token": "platform-secret"},
                json=payload,
            )
        app.dependency_overrides.clear()

        assert response.status_code == 200
        assert response.json()["queued_events"] == 1
        event = next(
            record for record in session.records
            if hasattr(record, "provider_update_id")
        )
        resolved_tenants.append(event.tenant_id)
        context = next(
            record for record in session.records
            if hasattr(record, "telegram_chat_id")
        )

    assert resolved_tenants == [tenant_a, tenant_b]
    assert context.tenant_id == tenant_b
    assert [job["tenant_id"] for job in jobs] == [tenant_a, tenant_b]
    assert jobs[0]["idempotency_key"] != jobs[1]["idempotency_key"]
