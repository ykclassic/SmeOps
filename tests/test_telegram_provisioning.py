from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import SecretStr

from app.api import telegram as telegram_api
from app.schemas.telegram import TelegramConnectionProvision


class FakeResult:
    def scalar_one_or_none(self):
        return None


class FakeSession:
    def __init__(self):
        self.records = []
        self.commits = 0

    async def execute(self, _statement):
        return FakeResult()

    def add(self, value):
        self.records.append(value)

    async def commit(self):
        self.commits += 1


@pytest.mark.asyncio
async def test_owner_bot_provisioning_verifies_bot_and_registers_webhook(monkeypatch):
    tenant_id = uuid4()
    user = SimpleNamespace(tenant_id=tenant_id, id=uuid4())
    session = FakeSession()
    registered = []
    encrypted = []

    async def fake_get_identity(*, bot_token):
        assert bot_token == "123456:valid-test-token"
        return SimpleNamespace(bot_id="123456789", username="smeops_test_bot")

    async def fake_register(connection):
        registered.append(connection)

    monkeypatch.setattr(
        telegram_api,
        "get_settings",
        lambda: SimpleNamespace(
            whatsapp_token_encryption_key=object(),
            public_api_base_url="https://api.example.com",
            app_secret_key=SecretStr("s" * 40),
        ),
    )
    monkeypatch.setattr(telegram_api, "get_bot_identity", fake_get_identity)
    monkeypatch.setattr(telegram_api, "_register_connection_webhook", fake_register)
    monkeypatch.setattr(
        telegram_api,
        "encrypt_telegram_secret",
        lambda value: encrypted.append(value) or "encrypted-token",
    )

    response = await telegram_api.provision_telegram_connection(
        TelegramConnectionProvision(
            bot_token=SecretStr("123456:valid-test-token")
        ),
        session,
        user,
    )

    assert response.bot_id == "123456789"
    assert response.bot_username == "smeops_test_bot"
    assert response.status == "active"
    assert encrypted == ["123456:valid-test-token"]
    assert len(registered) == 1
    assert registered[0].tenant_id == tenant_id
    assert registered[0].status == "active"
    assert registered[0].bot_token_encrypted == "encrypted-token"
    assert len(registered[0].webhook_secret_hash) == 64
    assert session.commits == 2
