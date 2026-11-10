from types import SimpleNamespace
from uuid import uuid4

import pytest
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


class FakePlatformSession:
    def __init__(self, *results, commit_error=None, flush_error=None):
        self.results = list(results)
        self.events = []
        self.committed = False
        self.commit_error = commit_error
        self.flush_error = flush_error

    async def execute(self, _statement):
        value = self.results.pop(0) if self.results else None
        return FakeResult(value)

    def begin_nested(self):
        return FakeNested()

    def add(self, value):
        self.events.append(value)

    async def flush(self):
        if self.flush_error:
            raise self.flush_error

    async def commit(self):
        if self.commit_error:
            raise self.commit_error
        self.committed = True


def _platform_bot():
    return SimpleNamespace(
        id=uuid4(),
        bot_id="9016622451",
        webhook_secret_hash=hash_telegram_webhook_secret("correct-secret"),
    )


def _message_update(update_id=101, text="hello"):
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "chat": {"id": 555, "type": "private"},
            "from": {"id": 555, "is_bot": False},
            "text": text,
        },
    }


def _client(session, *, raise_server_exceptions=True):
    async def override_session():
        yield session

    app.dependency_overrides[get_db_session] = override_session
    return TestClient(app, raise_server_exceptions=raise_server_exceptions)


def _post(client, payload, secret="correct-secret"):
    return client.post(
        "/webhooks/telegram/platform",
        headers={"X-Telegram-Bot-Api-Secret-Token": secret},
        json=payload,
    )


def test_platform_webhook_rejects_invalid_secret():
    session = FakePlatformSession(_platform_bot())
    with _client(session) as client:
        response = _post(client, _message_update(), secret="wrong-secret")
    app.dependency_overrides.clear()

    assert response.status_code == 401
    assert session.events == []
    assert not session.committed


def test_platform_webhook_rejects_malformed_update_id():
    session = FakePlatformSession(_platform_bot())
    with _client(session) as client:
        response = _post(client, {"update_id": "invalid"})
    app.dependency_overrides.clear()

    assert response.status_code == 400
    assert session.events == []
    assert not session.committed


def test_platform_webhook_ignores_unsupported_update_explicitly():
    session = FakePlatformSession(_platform_bot())
    with _client(session) as client:
        response = _post(client, {"update_id": 102, "callback_query": {"id": "opaque"}})
    app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {"status": "ignored", "queued_events": 0}
    assert session.events == []
    assert not session.committed


def test_platform_webhook_returns_duplicate_only_for_existing_event():
    existing = SimpleNamespace(
        id=uuid4(),
        payload_sha256="not-the-current-payload-hash",
    )
    session = FakePlatformSession(_platform_bot(), existing)
    with _client(session) as client:
        response = _post(client, _message_update(update_id=103))
    app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {"status": "duplicate", "queued_events": 0}
    assert session.events == []
    assert not session.committed


def test_platform_webhook_commits_event_and_job_before_acknowledging(monkeypatch):
    tenant_id = uuid4()
    context = SimpleNamespace(tenant_id=tenant_id)
    session = FakePlatformSession(_platform_bot(), None, context)
    job_id = uuid4()
    queued = []

    async def fake_enqueue(_session, **kwargs):
        queued.append(kwargs)
        return SimpleNamespace(id=job_id)

    monkeypatch.setattr(telegram_api, "encrypt_telegram_secret", lambda _value: "encrypted")
    monkeypatch.setattr(telegram_api, "enqueue_job", fake_enqueue)
    with _client(session) as client:
        response = _post(client, _message_update(update_id=104))
    app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {"status": "accepted", "queued_events": 1}
    assert session.committed
    assert len(session.events) == 1
    event = session.events[0]
    assert event.provider_update_id == "104"
    assert event.tenant_id == tenant_id
    assert queued[0]["job_type"] == "telegram.accept_inbound"
    assert queued[0]["tenant_id"] == tenant_id
    assert queued[0]["payload"] == {"webhook_event_id": str(event.id)}


@pytest.mark.parametrize("failure_point", ["enqueue", "commit"])
def test_platform_webhook_never_returns_200_when_persistence_fails(
    monkeypatch,
    failure_point,
):
    tenant_id = uuid4()
    context = SimpleNamespace(tenant_id=tenant_id)
    session = FakePlatformSession(
        _platform_bot(),
        None,
        context,
        commit_error=RuntimeError("simulated database commit failure")
        if failure_point == "commit"
        else None,
    )

    async def fake_enqueue(_session, **_kwargs):
        if failure_point == "enqueue":
            raise RuntimeError("simulated enqueue failure")
        return SimpleNamespace(id=uuid4())

    monkeypatch.setattr(telegram_api, "encrypt_telegram_secret", lambda _value: "encrypted")
    monkeypatch.setattr(telegram_api, "enqueue_job", fake_enqueue)
    with _client(session, raise_server_exceptions=False) as client:
        response = _post(client, _message_update(update_id=105))
    app.dependency_overrides.clear()

    assert response.status_code == 500
    assert not session.committed


@pytest.mark.parametrize(
    ("payload", "extra_results"),
    [
        ({"update_id": 201, "callback_query": {"id": "opaque"}}, ()),
        (
            {
                **_message_update(update_id=202),
                "message": {
                    "chat": {"id": -10, "type": "group"},
                    "from": {"id": 555, "is_bot": False},
                },
            },
            (),
        ),
        (
            {
                **_message_update(update_id=203),
                "message": {
                    "chat": {"id": 555, "type": "private"},
                    "from": {"id": 555, "is_bot": True},
                },
            },
            (),
        ),
        (
            {
                **_message_update(update_id=204),
                "message": {
                    "chat": {"id": 555, "type": "private"},
                    "from": {"id": 556, "is_bot": False},
                },
            },
            (),
        ),
        (_message_update(update_id=205, text="/start"), ()),
        (_message_update(update_id=206), (None,)),
    ],
)
def test_platform_webhook_acknowledges_unroutable_updates_as_ignored(
    payload,
    extra_results,
):
    # First query loads the platform bot; the next checks the event de-duplication
    # record. A third result is needed only when resolving an existing chat context.
    results = [_platform_bot(), None, *extra_results]
    session = FakePlatformSession(*results)
    with _client(session) as client:
        response = _post(client, payload)
    app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {"status": "ignored", "queued_events": 0}
    assert session.events == []
    assert not session.committed


def test_platform_webhook_rejects_invalid_json():
    session = FakePlatformSession(_platform_bot())
    async def override_session():
        yield session

    app.dependency_overrides[get_db_session] = override_session
    with TestClient(app) as client:
        response = client.post(
            "/webhooks/telegram/platform",
            headers={"X-Telegram-Bot-Api-Secret-Token": "correct-secret"},
            content=b"{not-json",
        )
    app.dependency_overrides.clear()

    assert response.status_code == 400
    assert session.events == []
    assert not session.committed



def test_platform_webhook_rejects_when_platform_bot_is_not_configured():
    session = FakePlatformSession(None)
    with _client(session) as client:
        response = _post(client, _message_update())
    app.dependency_overrides.clear()

    assert response.status_code == 404
    assert session.events == []
    assert not session.committed


def test_platform_webhook_rejects_non_object_json_payload():
    session = FakePlatformSession(_platform_bot())
    async def override_session():
        yield session

    app.dependency_overrides[get_db_session] = override_session
    with TestClient(app) as client:
        response = client.post(
            "/webhooks/telegram/platform",
            headers={"X-Telegram-Bot-Api-Secret-Token": "correct-secret"},
            content=b'["not", "an", "update"]',
        )
    app.dependency_overrides.clear()

    assert response.status_code == 400
    assert session.events == []
    assert not session.committed


def test_platform_webhook_rejects_oversized_payload():
    session = FakePlatformSession(_platform_bot())
    async def override_session():
        yield session

    app.dependency_overrides[get_db_session] = override_session
    with TestClient(app) as client:
        response = client.post(
            "/webhooks/telegram/platform",
            headers={"X-Telegram-Bot-Api-Secret-Token": "correct-secret"},
            content=b"x" * (telegram_api.MAX_TELEGRAM_WEBHOOK_BYTES + 1),
        )
    app.dependency_overrides.clear()

    assert response.status_code == 413
    assert session.events == []
    assert not session.committed


def test_platform_webhook_does_not_acknowledge_event_insert_failure(
    monkeypatch,
):
    tenant_id = uuid4()
    context = SimpleNamespace(tenant_id=tenant_id)
    session = FakePlatformSession(
        _platform_bot(),
        None,
        context,
        flush_error=RuntimeError("simulated event insert failure"),
    )
    monkeypatch.setattr(telegram_api, "encrypt_telegram_secret", lambda _value: "encrypted")
    with _client(session, raise_server_exceptions=False) as client:
        response = _post(client, _message_update(update_id=207))
    app.dependency_overrides.clear()

    assert response.status_code == 500
    assert not session.committed
