from contextlib import nullcontext
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.services import telegram_outbound


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
    def __init__(self, values):
        self.values = list(values)
        self.records = []
        self.committed = False
        self.no_autoflush = nullcontext()

    async def execute(self, _statement):
        return FakeResult(self.values.pop(0))

    def begin_nested(self):
        return FakeNested()

    def add(self, value):
        self.records.append(value)

    async def flush(self):
        return None

    async def commit(self):
        self.committed = True


@pytest.mark.asyncio
async def test_enqueue_telegram_text_creates_ledger_history_and_job(monkeypatch):
    tenant_id = uuid4()
    conversation = SimpleNamespace(
        id=uuid4(),
        tenant_id=tenant_id,
        telegram_chat_id="-100123456789",
    )
    tenant = SimpleNamespace(id=tenant_id, status="active")
    platform_bot = SimpleNamespace(singleton_key="platform", status="active")
    session = FakeSession([tenant, None, conversation, platform_bot])
    queued = []

    async def fake_enqueue(_session, **kwargs):
        queued.append(kwargs)

    monkeypatch.setattr(
        telegram_outbound,
        "encrypt_telegram_secret",
        lambda value: f"encrypted:{value}",
    )
    monkeypatch.setattr(telegram_outbound, "enqueue_job", fake_enqueue)

    result = await telegram_outbound.enqueue_telegram_text(
        session,
        tenant_id=tenant_id,
        conversation_id=conversation.id,
        body=" Your appointment is confirmed. ",
        idempotency_key="telegram-idempotency-1",
    )

    assert result.status == "queued"
    assert result.recipient_chat_id == "-100123456789"
    assert result.body_encrypted == "encrypted:Your appointment is confirmed."
    assert not session.committed  # transaction is committed by the calling workflow
    assert len(session.records) == 3
    assert queued[0]["job_type"] == "telegram.send_text"
    assert queued[0]["tenant_id"] == tenant_id
    assert queued[0]["payload"] == {"message_id": str(result.id)}


@pytest.mark.asyncio
async def test_enqueue_telegram_text_rejects_reused_key_with_different_content(monkeypatch):
    existing = SimpleNamespace(
        conversation_id=uuid4(),
        body_encrypted="ciphertext",
        deliver_after_deactivation=False,
    )
    tenant_id = uuid4()
    session = FakeSession([SimpleNamespace(id=tenant_id, status="active"), existing])
    monkeypatch.setattr(
        telegram_outbound,
        "decrypt_telegram_secret",
        lambda _value: "original content",
    )

    with pytest.raises(telegram_outbound.TelegramOutboundIdempotencyConflict):
        await telegram_outbound.enqueue_telegram_text(
            session,
            tenant_id=tenant_id,
            conversation_id=uuid4(),
            body="different content",
            idempotency_key="telegram-idempotency-1",
        )



@pytest.mark.asyncio
async def test_telegram_worker_delivers_preserved_message_after_deactivation(monkeypatch):
    from app.jobs import telegram_outbound as worker
    from app.jobs.queue import ClaimedJob
    tenant_id = uuid4()
    message = SimpleNamespace(
        id=uuid4(), tenant_id=tenant_id, status="queued", deliver_after_deactivation=True,
        recipient_chat_id="998877", body_encrypted="cipher:Appointment confirmed",
        reply_markup=None, provider_message_id=None, last_error_code=None, sent_at=None,
    )
    history = SimpleNamespace(provider_message_id=f"outbound:{message.id}", delivery_status="queued")
    bot = SimpleNamespace(bot_token_encrypted="cipher:token")
    first, second = FakeSession([
        SimpleNamespace(id=tenant_id, status="deactivated"), message, bot
    ]), FakeSession([message, history])

    class Context:
        def __init__(self, session): self.session = session
        async def __aenter__(self): return self.session
        async def __aexit__(self, *_args): return False

    sessions = iter([first, second])
    monkeypatch.setattr(worker, "AsyncSessionFactory", lambda: Context(next(sessions)))
    monkeypatch.setattr(worker, "decrypt_telegram_secret", lambda v: v.removeprefix("cipher:"))
    sent = []
    async def fake_send(**kwargs):
        sent.append(kwargs)
        return SimpleNamespace(provider_message_id="provider-123")
    monkeypatch.setattr(worker, "send_text_message", fake_send)
    await worker.send_outbound_telegram_job(ClaimedJob(
        id=uuid4(), tenant_id=tenant_id, job_type="telegram.send_text",
        payload={"message_id": str(message.id)}, attempts=1, max_attempts=2,
    ))
    assert len(sent) == 1
    assert message.status == "sent"
    assert history.delivery_status == "sent"
    assert first.committed and second.committed


@pytest.mark.asyncio
async def test_telegram_worker_suppresses_preserved_message_for_suspended_tenant(monkeypatch):
    from app.jobs import telegram_outbound as worker
    from app.jobs.queue import ClaimedJob
    tenant_id = uuid4()
    message = SimpleNamespace(
        id=uuid4(), tenant_id=tenant_id, status="queued", deliver_after_deactivation=True,
        provider_message_id=None, last_error_code=None,
    )
    history = SimpleNamespace(provider_message_id=f"outbound:{message.id}", delivery_status="queued")
    session = FakeSession([SimpleNamespace(id=tenant_id, status="suspended"), message, history])

    class Context:
        async def __aenter__(self): return session
        async def __aexit__(self, *_args): return False

    monkeypatch.setattr(worker, "AsyncSessionFactory", lambda: Context())
    async def must_not_send(**_kwargs):
        raise AssertionError("suspended tenant messages must be suppressed")
    monkeypatch.setattr(worker, "send_text_message", must_not_send)
    await worker.send_outbound_telegram_job(ClaimedJob(
        id=uuid4(), tenant_id=tenant_id, job_type="telegram.send_text",
        payload={"message_id": str(message.id)}, attempts=1, max_attempts=2,
    ))
    assert message.status == "failed"
    assert message.last_error_code == "tenant_inactive"
    assert history.delivery_status == "failed"
    assert session.committed
