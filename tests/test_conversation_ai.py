import json

import httpx
import pytest

from app.core.config import get_settings
from app.integrations.conversation_ai import (
    ConversationAIUnavailable,
    classify_inbound_intent,
)


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class FakeAsyncClient:
    def __init__(self, response):
        self.response = response
        self.request = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def post(self, url, *, headers, json):
        self.request = {"url": url, "headers": headers, "json": json}
        return self.response


@pytest.mark.asyncio
async def test_openai_interpreter_uses_strict_structured_output(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-api-key")
    monkeypatch.setenv("OPENAI_MODEL", "test-model")
    get_settings.cache_clear()
    fake_client = FakeAsyncClient(
        FakeResponse(
            {
                "output_text": json.dumps(
                    {"intent": "booking_start", "confidence": 0.98}
                )
            }
        )
    )
    monkeypatch.setattr(httpx, "AsyncClient", lambda **_kwargs: fake_client)
    try:
        result = await classify_inbound_intent("I would like to make an appointment")
        assert result is not None
        assert result.intent == "booking_start"
        assert result.confidence == 0.98
        assert fake_client.request["url"] == "https://api.openai.com/v1/responses"
        assert fake_client.request["json"]["text"]["format"]["strict"] is True
        assert fake_client.request["json"]["store"] is False
        assert "Authorization" in fake_client.request["headers"]
    finally:
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_openai_interpreter_is_disabled_without_credentials(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    get_settings.cache_clear()
    try:
        assert await classify_inbound_intent("Book an appointment") is None
    finally:
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_openai_interpreter_fails_closed_to_caller_on_http_error(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-api-key")
    monkeypatch.setenv("OPENAI_MODEL", "test-model")
    get_settings.cache_clear()

    class FailingClient(FakeAsyncClient):
        async def post(self, *_args, **_kwargs):
            raise httpx.TimeoutException("timeout")

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **_kwargs: FailingClient(FakeResponse({})),
    )
    try:
        with pytest.raises(ConversationAIUnavailable):
            await classify_inbound_intent("Can I get an appointment?")
    finally:
        get_settings.cache_clear()
