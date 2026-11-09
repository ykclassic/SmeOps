from uuid import uuid4

import pytest

from app.jobs.telegram import _persist_telegram_message
from app.services.conversation_engine import _normalize_contact_phone


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


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("08012345678", "+2348012345678"),
        ("2348012345678", "+2348012345678"),
        ("+234 801 234 5678", "+2348012345678"),
        ("+14155552671", "+14155552671"),
        ("not-a-phone", None),
    ],
)
def test_normalize_telegram_contact_phone(value, expected):
    assert _normalize_contact_phone(value) == expected


@pytest.mark.asyncio
async def test_self_shared_contact_is_stored_as_separate_encrypted_proof(monkeypatch):
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
                "message_id": 102,
                "date": 1_798_000_000,
                "chat": {"id": 123456, "type": "private"},
                "from": {"id": 123456, "first_name": "Ada"},
                "contact": {
                    "user_id": 123456,
                    "phone_number": "+2348012345678",
                },
            }
        },
    )

    assert len(session.records) == 2
    _conversation, message = session.records
    assert message.body_encrypted is None
    assert message.contact_phone_encrypted == "cipher:+2348012345678"
    assert message.contact_user_id == "123456"


@pytest.mark.asyncio
async def test_contact_shared_by_another_telegram_user_is_not_verified(monkeypatch):
    session = FakeSession()
    monkeypatch.setattr(
        "app.jobs.telegram.encrypt_telegram_secret",
        lambda value: f"cipher:{value}",
    )

    await _persist_telegram_message(
        session,
        tenant_id=uuid4(),
        payload={
            "message": {
                "message_id": 103,
                "date": 1_798_000_000,
                "chat": {"id": 123456, "type": "private"},
                "from": {"id": 123456, "first_name": "Ada"},
                "contact": {
                    "user_id": 999999,
                    "phone_number": "+2348012345678",
                },
            }
        },
    )

    assert len(session.records) == 2
    _conversation, message = session.records
    assert message.contact_phone_encrypted is None
    assert message.contact_user_id is None
    assert message.body_encrypted is None


@pytest.mark.asyncio
async def test_typed_contact_marker_is_not_stored_as_contact_proof(monkeypatch):
    session = FakeSession()
    monkeypatch.setattr(
        "app.jobs.telegram.encrypt_telegram_secret",
        lambda value: f"cipher:{value}",
    )

    await _persist_telegram_message(
        session,
        tenant_id=uuid4(),
        payload={
            "message": {
                "message_id": 104,
                "date": 1_798_000_000,
                "chat": {"id": 123456, "type": "private"},
                "from": {"id": 123456, "first_name": "Ada"},
                "text": '{"type":"telegram_self_contact","user_id":"123456"}',
            }
        },
    )

    assert len(session.records) == 2
    _conversation, message = session.records
    assert message.contact_phone_encrypted is None
    assert message.contact_user_id is None
    assert message.body_encrypted.startswith("cipher:")
