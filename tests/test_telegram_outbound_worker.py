from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.jobs import telegram_outbound
from app.jobs.queue import ClaimedJob


class FakeResult:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class FakeSession:
    def __init__(self, values):
        self.values = list(values)
        self.commits = 0

    async def execute(self, _statement):
        return FakeResult(self.values.pop(0))

    async def commit(self):
        self.commits += 1


class SessionContext:
    def __init__(self, session):
        self.session = session

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, *_args):
        return False


@pytest.mark.asyncio
async def test_outbound_worker_sends_and_updates_durable_ledger(monkeypatch):
    tenant_id = uuid4()
    message_id = uuid4()
    message = SimpleNamespace(
        id=message_id,
        tenant_id=tenant_id,
        status="queued",
        recipient_chat_id="-100123456789",
        body_encrypted="encrypted:Hello",
        reply_markup=None,
        provider_message_id=None,
        last_error_code=None,
        sent_at=None,
    )
    connection = SimpleNamespace(
        tenant_id=tenant_id,
        status="active",
        bot_token_encrypted="encrypted:bot-token",
    )
    history = SimpleNamespace(
        provider_message_id=f"outbound:{message_id}",
        delivery_status="queued",
    )
    first = FakeSession([message, connection])
    second = FakeSession([message, history])
    sessions = iter([first, second])
    sent = []

    def fake_session_factory():
        return SessionContext(next(sessions))

    def fake_decrypt(value):
        return value.removeprefix("encrypted:")

    async def fake_send(*, bot_token, chat_id, body, reply_markup=None):
        sent.append((bot_token, chat_id, body, reply_markup))
        return SimpleNamespace(provider_message_id="456")

    monkeypatch.setattr(telegram_outbound, "AsyncSessionFactory", fake_session_factory)
    monkeypatch.setattr(telegram_outbound, "decrypt_telegram_secret", fake_decrypt)
    monkeypatch.setattr(telegram_outbound, "send_text_message", fake_send)

    await telegram_outbound.send_outbound_telegram_job(
        ClaimedJob(
            id=uuid4(),
            tenant_id=tenant_id,
            job_type="telegram.send_text",
            payload={"message_id": str(message_id)},
            attempts=1,
            max_attempts=2,
        )
    )

    assert sent == [("bot-token", "-100123456789", "Hello")]
    assert message.status == "sent"
    assert message.provider_message_id == "-100123456789:456"
    assert history.provider_message_id == "-100123456789:456"
    assert history.delivery_status == "sent"
    assert message.sent_at is not None
    assert first.commits == 1
    assert second.commits == 1


@pytest.mark.asyncio
async def test_outbound_worker_does_not_resend_ambiguous_sending_record(monkeypatch):
    tenant_id = uuid4()
    message_id = uuid4()
    message = SimpleNamespace(
        id=message_id,
        tenant_id=tenant_id,
        status="sending",
        provider_message_id=None,
        last_error_code=None,
    )
    history = SimpleNamespace(
        provider_message_id=f"outbound:{message_id}",
        delivery_status="sending",
    )
    session = FakeSession([message, history])
    sent = False

    def fake_session_factory():
        return SessionContext(session)

    async def forbidden_send(**_kwargs):
        nonlocal sent
        sent = True

    monkeypatch.setattr(telegram_outbound, "AsyncSessionFactory", fake_session_factory)
    monkeypatch.setattr(telegram_outbound, "send_text_message", forbidden_send)

    await telegram_outbound.send_outbound_telegram_job(
        ClaimedJob(
            id=uuid4(),
            tenant_id=tenant_id,
            job_type="telegram.send_text",
            payload={"message_id": str(message_id)},
            attempts=2,
            max_attempts=2,
        )
    )

    assert not sent
    assert message.status == "unknown"
    assert message.last_error_code == "previous_send_outcome_unknown"
    assert history.delivery_status == "unknown"
    assert session.commits == 1
