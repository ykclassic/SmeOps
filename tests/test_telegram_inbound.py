from uuid import uuid4

import pytest

from app.jobs.telegram import _persist_telegram_message


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
    def __init__(self):
        self.execute_count = 0
        self.records = []

    async def execute(self, _statement):
        self.execute_count += 1
        return FakeResult(None)

    def begin_nested(self):
        return FakeNested()

    def add(self, value):
        self.records.append(value)

    async def flush(self):
        return None


@pytest.mark.asyncio
async def test_inbound_update_persists_chat_identity_and_encrypted_text(monkeypatch):
    session = FakeSession()
    tenant_id = uuid4()
    monkeypatch.setattr(
        "app.jobs.telegram.encrypt_telegram_secret",
        lambda value: f"cipher:{value}",
    )

    await _persist_telegram_message(
        session,
        tenant_id=tenant_id,
        payload={
            "message": {
                "message_id": 101,
                "date": 1_798_000_000,
                "chat": {"id": -1001234567890},
                "from": {"id": 123456, "first_name": "Ada", "last_name": "Okafor"},
                "text": "I need an appointment",
            }
        },
    )

    assert len(session.records) == 2
    conversation, message = session.records
    assert conversation.tenant_id == tenant_id
    assert conversation.telegram_chat_id == "-1001234567890"
    assert conversation.telegram_user_id == "123456"
    assert conversation.customer_name == "Ada Okafor"
    assert message.conversation_id == conversation.id
    assert message.provider_message_id == "-1001234567890:101"
    assert message.body_encrypted == "cipher:I need an appointment"


@pytest.mark.asyncio
async def test_inbound_update_ignores_invalid_identity_without_persisting(monkeypatch):
    session = FakeSession()
    await _persist_telegram_message(
        session,
        tenant_id=uuid4(),
        payload={
            "message": {
                "message_id": 0,
                "chat": {"id": 123},
                "from": {"id": 456},
                "text": "Hello",
            }
        },
    )
    assert session.records == []
