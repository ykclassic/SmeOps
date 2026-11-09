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
    connection = SimpleNamespace(tenant_id=tenant_id, status="active")
    session = FakeSession([None, conversation, connection])
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
    )
    session = FakeSession([existing])
    monkeypatch.setattr(
        telegram_outbound,
        "decrypt_telegram_secret",
        lambda _value: "original content",
    )

    with pytest.raises(telegram_outbound.TelegramOutboundIdempotencyConflict):
        await telegram_outbound.enqueue_telegram_text(
            session,
            tenant_id=uuid4(),
            conversation_id=uuid4(),
            body="different content",
            idempotency_key="telegram-idempotency-1",
        )
