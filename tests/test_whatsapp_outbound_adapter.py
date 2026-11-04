import httpx
import pytest

from app.integrations.whatsapp_cloud import (
    WhatsAppDeliveryUnknown,
    WhatsAppSendRejected,
    send_text_message,
)


class FakeAsyncClient:
    def __init__(self, response: httpx.Response):
        self.response = response
        self.request = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def post(self, url, *, headers, json):
        self.request = (url, headers, json)
        return self.response


@pytest.mark.asyncio
async def test_send_text_message_returns_provider_message_id(monkeypatch):
    client = FakeAsyncClient(
        httpx.Response(200, json={"messages": [{"id": "wamid.test-123"}]})
    )
    monkeypatch.setattr(
        "app.integrations.whatsapp_cloud.httpx.AsyncClient",
        lambda **_kwargs: client,
    )

    result = await send_text_message(
        api_version="v25.0",
        phone_number_id="12345678901",
        access_token="secret-token",
        recipient="2348012345678",
        body="Your appointment is confirmed.",
    )

    assert result.provider_message_id == "wamid.test-123"
    assert client.request[0].endswith("/v25.0/12345678901/messages")
    assert client.request[1]["Authorization"] == "Bearer secret-token"
    assert client.request[2]["to"] == "2348012345678"


@pytest.mark.asyncio
async def test_send_text_message_treats_provider_4xx_as_rejection(monkeypatch):
    client = FakeAsyncClient(httpx.Response(400, json={"error": {"message": "invalid"}}))
    monkeypatch.setattr(
        "app.integrations.whatsapp_cloud.httpx.AsyncClient",
        lambda **_kwargs: client,
    )

    with pytest.raises(WhatsAppSendRejected):
        await send_text_message(
            api_version="v25.0",
            phone_number_id="12345678901",
            access_token="secret-token",
            recipient="2348012345678",
            body="Hello",
        )


@pytest.mark.asyncio
async def test_send_text_message_treats_provider_5xx_as_unknown(monkeypatch):
    client = FakeAsyncClient(httpx.Response(503, json={"error": {"message": "temporary"}}))
    monkeypatch.setattr(
        "app.integrations.whatsapp_cloud.httpx.AsyncClient",
        lambda **_kwargs: client,
    )

    with pytest.raises(WhatsAppDeliveryUnknown):
        await send_text_message(
            api_version="v25.0",
            phone_number_id="12345678901",
            access_token="secret-token",
            recipient="2348012345678",
            body="Hello",
        )


@pytest.mark.asyncio
async def test_send_text_message_treats_malformed_success_as_unknown(monkeypatch):
    client = FakeAsyncClient(httpx.Response(200, json={"messages": []}))
    monkeypatch.setattr(
        "app.integrations.whatsapp_cloud.httpx.AsyncClient",
        lambda **_kwargs: client,
    )

    with pytest.raises(WhatsAppDeliveryUnknown):
        await send_text_message(
            api_version="v25.0",
            phone_number_id="12345678901",
            access_token="secret-token",
            recipient="2348012345678",
            body="Hello",
        )
