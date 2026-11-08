import httpx
import pytest

from app.integrations import telegram_bot


@pytest.mark.asyncio
async def test_get_webhook_info_returns_url_pending_count_and_error(monkeypatch):
    class Response:
        status_code = 200

        @staticmethod
        def json():
            return {
                "ok": True,
                "result": {
                    "url": "https://api.example.com/webhooks/telegram/platform",
                    "pending_update_count": 3,
                    "last_error_date": 1_798_000_000,
                    "last_error_message": "delivery failed",
                },
            }

    class Client:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def post(self, *_args, **_kwargs):
            return Response()

    monkeypatch.setattr(telegram_bot.httpx, "AsyncClient", Client)
    result = await telegram_bot.get_webhook_info(bot_token="123456:fake-test-token")

    assert result.url.endswith("/webhooks/telegram/platform")
    assert result.pending_update_count == 3
    assert result.last_error_date == 1_798_000_000
    assert result.last_error_message == "delivery failed"


@pytest.mark.asyncio
async def test_get_webhook_info_rejects_invalid_pending_count(monkeypatch):
    class Response:
        status_code = 200

        @staticmethod
        def json():
            return {"ok": True, "result": {"url": "", "pending_update_count": -1}}

    class Client:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def post(self, *_args, **_kwargs):
            return Response()

    monkeypatch.setattr(telegram_bot.httpx, "AsyncClient", Client)
    with pytest.raises(telegram_bot.TelegramAPIError):
        await telegram_bot.get_webhook_info(bot_token="123456:fake-test-token")
