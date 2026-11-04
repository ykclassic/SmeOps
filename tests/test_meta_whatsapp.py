from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.integrations.meta_whatsapp import (
    MetaGraphAPIError,
    exchange_embedded_signup_code,
    register_phone_number,
    subscribe_app_to_waba,
    verify_phone_number_belongs_to_waba,
)


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code
        self.is_error = status_code >= 400

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

    async def request(self, method, url, **kwargs):
        self.requests.append({"method": method, "url": url, **kwargs})
        return self.responses.pop(0)


@pytest.mark.asyncio
async def test_exchange_signup_code_returns_expiry_and_uses_server_credentials(monkeypatch):
    FakeAsyncClient.responses = [
        FakeResponse({"access_token": "test-meta-business-token-123456", "expires_in": 3600})
    ]
    FakeAsyncClient.requests = []
    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)

    token = await exchange_embedded_signup_code(
        api_version="v26.0",
        app_id="123456789",
        app_secret="server-only-meta-secret",
        code="short-lived-code",
    )

    assert token.value == "test-meta-business-token-123456"
    assert token.expires_at is not None
    assert token.expires_at > datetime.now(UTC)
    request = FakeAsyncClient.requests[0]
    assert request["method"] == "GET"
    assert request["url"].endswith("/v26.0/oauth/access_token")
    assert request["params"]["client_secret"] == "server-only-meta-secret"
    assert request["params"]["code"] == "short-lived-code"


@pytest.mark.asyncio
async def test_phone_number_must_belong_to_selected_waba(monkeypatch):
    FakeAsyncClient.responses = [
        FakeResponse(
            {
                "data": [
                    {
                        "id": "123456789012",
                        "display_phone_number": "+234 801 234 5678",
                        "verified_name": "Studio Ada",
                    }
                ]
            }
        )
    ]
    FakeAsyncClient.requests = []
    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)

    phone = await verify_phone_number_belongs_to_waba(
        api_version="v26.0",
        access_token="test-meta-business-token",
        waba_id="987654321098",
        phone_number_id="123456789012",
    )

    assert phone.display_phone_number == "+234 801 234 5678"
    assert phone.verified_name == "Studio Ada"
    assert FakeAsyncClient.requests[0]["url"].endswith("/987654321098/phone_numbers")


@pytest.mark.asyncio
async def test_phone_number_outside_waba_is_rejected(monkeypatch):
    FakeAsyncClient.responses = [FakeResponse({"data": [{"id": "111111111111"}]})]
    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)

    with pytest.raises(MetaGraphAPIError) as exc_info:
        await verify_phone_number_belongs_to_waba(
            api_version="v26.0",
            access_token="test-meta-business-token",
            waba_id="987654321098",
            phone_number_id="123456789012",
        )
    assert exc_info.value.error_code == "meta_phone_not_in_waba"


@pytest.mark.asyncio
async def test_waba_subscription_and_phone_registration_are_server_side(monkeypatch):
    FakeAsyncClient.responses = [FakeResponse({"success": True}), FakeResponse({"success": True})]
    FakeAsyncClient.requests = []
    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)

    await subscribe_app_to_waba(
        api_version="v26.0",
        access_token="test-meta-business-token",
        waba_id="987654321098",
    )
    await register_phone_number(
        api_version="v26.0",
        access_token="test-meta-business-token",
        phone_number_id="123456789012",
        two_step_pin="123456",
    )

    assert FakeAsyncClient.requests[0]["method"] == "POST"
    assert FakeAsyncClient.requests[0]["url"].endswith("/987654321098/subscribed_apps")
    assert FakeAsyncClient.requests[1]["url"].endswith("/123456789012/register")
    assert FakeAsyncClient.requests[1]["json"]["messaging_product"] == "whatsapp"
    assert FakeAsyncClient.requests[1]["json"]["pin"] == "123456"


@pytest.mark.asyncio
async def test_meta_graph_error_does_not_expose_provider_message(monkeypatch):
    FakeAsyncClient.responses = [
        FakeResponse(
            {"error": {"code": 190, "message": "sensitive provider response text"}},
            status_code=400,
        )
    ]
    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)

    with pytest.raises(MetaGraphAPIError) as exc_info:
        await exchange_embedded_signup_code(
            api_version="v26.0",
            app_id="123456789",
            app_secret="server-only-meta-secret",
            code="invalid-code",
        )
    assert exc_info.value.error_code == "meta_graph_190"
    assert "sensitive provider response text" not in str(exc_info.value)
