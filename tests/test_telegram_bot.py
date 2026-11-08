import httpx
import pytest

from app.integrations.telegram_bot import (
    TelegramDeliveryUnknown,
    TelegramSendRejected,
    get_bot_identity,
    register_webhook,
    send_text_message,
)


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code

    def json(self):
        return self.payload


class FakeAsyncClient:
    responses = []
    requests = []

    def __init__(self, **_kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def post(self, url, **kwargs):
        self.requests.append({"url": url, **kwargs})
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


@pytest.mark.asyncio
async def test_send_text_uses_bot_api_and_returns_provider_message_id(monkeypatch):
    FakeAsyncClient.responses = [FakeResponse({"ok": True, "result": {"message_id": 123}})]
    FakeAsyncClient.requests = []
    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)

    result = await send_text_message(
        bot_token="123456:valid-test-token",
        chat_id="987654321",
        body="Your appointment is confirmed.",
    )

    assert result.provider_message_id == "123"
    request = FakeAsyncClient.requests[0]
    assert request["url"].endswith("/sendMessage")
    assert request["json"] == {
        "chat_id": "987654321",
        "text": "Your appointment is confirmed.",
    }


@pytest.mark.asyncio
async def test_send_rejected_is_not_retryable(monkeypatch):
    FakeAsyncClient.responses = [FakeResponse({"ok": False}, status_code=400)]
    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)

    with pytest.raises(TelegramSendRejected):
        await send_text_message(
            bot_token="123456:valid-test-token",
            chat_id="987654321",
            body="Hello",
        )


@pytest.mark.asyncio
async def test_transport_failure_is_delivery_unknown(monkeypatch):
    FakeAsyncClient.responses = [httpx.ConnectError("network unavailable")]
    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)

    with pytest.raises(TelegramDeliveryUnknown):
        await send_text_message(
            bot_token="123456:valid-test-token",
            chat_id="987654321",
            body="Hello",
        )


@pytest.mark.asyncio
async def test_malformed_success_response_is_delivery_unknown(monkeypatch):
    FakeAsyncClient.responses = [FakeResponse({"ok": True, "result": {}})]
    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)

    with pytest.raises(TelegramDeliveryUnknown):
        await send_text_message(
            bot_token="123456:valid-test-token",
            chat_id="987654321",
            body="Hello",
        )


def test_text_and_identifier_limits_are_enforced():
    with pytest.raises(ValueError):
        import asyncio

        asyncio.run(
            send_text_message(
                bot_token="token",
                chat_id="1",
                body="x" * 4097,
            )
        )



@pytest.mark.asyncio
async def test_get_me_returns_verified_bot_identity(monkeypatch):
    FakeAsyncClient.responses = [
        FakeResponse({"ok": True, "result": {"id": 123456789, "username": "smeops_test_bot"}})
    ]
    FakeAsyncClient.requests = []
    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)

    identity = await get_bot_identity(bot_token="123456:valid-test-token")

    assert identity.bot_id == "123456789"
    assert identity.username == "smeops_test_bot"
    assert FakeAsyncClient.requests[0]["url"].endswith("/getMe")


@pytest.mark.asyncio
async def test_register_webhook_sends_secret_and_preserves_pending_updates(monkeypatch):
    FakeAsyncClient.responses = [FakeResponse({"ok": True, "result": True})]
    FakeAsyncClient.requests = []
    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)

    await register_webhook(
        bot_token="123456:valid-test-token",
        webhook_url="https://api.example.com/webhooks/telegram/123456",
        secret_token="secure_webhook_secret_123",
    )

    request = FakeAsyncClient.requests[0]
    assert request["url"].endswith("/setWebhook")
    assert request["json"]["secret_token"] == "secure_webhook_secret_123"
    assert request["json"]["drop_pending_updates"] is False
    assert request["json"]["allowed_updates"] == ["message"]


@pytest.mark.asyncio
async def test_register_webhook_rejects_non_https_url(monkeypatch):
    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)
    with pytest.raises(ValueError):
        await register_webhook(
            bot_token="123456:valid-test-token",
            webhook_url="http://api.example.com/webhook",
            secret_token="secure_webhook_secret_123",
        )
