import pytest

from app.integrations import telegram_bot


class FakeResponse:
    status_code = 200

    def __init__(self, result):
        self.result = result

    def json(self):
        return {"ok": True, "result": self.result}


class FakeClient:
    responses = []
    requests = []

    def __init__(self, **_kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def post(self, url, **kwargs):
        self.requests.append({"url": url, **kwargs})
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


@pytest.mark.asyncio
async def test_get_webhook_info_returns_url_pending_count_and_error(monkeypatch):
    FakeClient.responses = [
        FakeResponse(
            {
                "url": "https://api.example.com/webhooks/telegram/platform",
                "pending_update_count": 3,
                "last_error_date": 1_798_000_000,
                "last_error_message": "delivery failed",
            }
        )
    ]
    monkeypatch.setattr(telegram_bot.httpx, "AsyncClient", FakeClient)
    result = await telegram_bot.get_webhook_info(bot_token="123456:fake-test-token")
    assert result.url.endswith("/webhooks/telegram/platform")
    assert result.pending_update_count == 3
    assert result.last_error_date == 1_798_000_000
    assert result.last_error_message == "delivery failed"


@pytest.mark.asyncio
async def test_get_webhook_info_rejects_invalid_pending_count(monkeypatch):
    FakeClient.responses = [FakeResponse({"url": "", "pending_update_count": -1})]
    monkeypatch.setattr(telegram_bot.httpx, "AsyncClient", FakeClient)
    with pytest.raises(telegram_bot.TelegramAPIError):
        await telegram_bot.get_webhook_info(bot_token="123456:fake-test-token")


@pytest.mark.asyncio
async def test_set_bot_commands_sets_private_scope_and_commands_menu(monkeypatch):
    FakeClient.requests = []
    FakeClient.responses = [
        FakeResponse(True),
        FakeResponse(True),
        FakeResponse(telegram_bot.TELEGRAM_CUSTOMER_COMMANDS),
        FakeResponse({"type": "commands"}),
    ]
    monkeypatch.setattr(telegram_bot.httpx, "AsyncClient", FakeClient)

    await telegram_bot.set_bot_commands(bot_token="123456:fake-test-token")

    requests = FakeClient.requests
    assert [request["url"].rsplit("/", 1)[-1] for request in requests] == [
        "setMyCommands",
        "setChatMenuButton",
        "getMyCommands",
        "getChatMenuButton",
    ]
    assert requests[0]["json"]["scope"] == {"type": "all_private_chats"}
    assert requests[0]["json"]["commands"] == telegram_bot.TELEGRAM_CUSTOMER_COMMANDS
    assert requests[1]["json"] == {"menu_button": {"type": "commands"}}
    assert requests[2]["json"]["scope"] == {"type": "all_private_chats"}


@pytest.mark.asyncio
async def test_set_bot_commands_fails_if_telegram_does_not_confirm_commands(monkeypatch):
    FakeClient.responses = [
        FakeResponse(True),
        FakeResponse(True),
        FakeResponse([]),
    ]
    monkeypatch.setattr(telegram_bot.httpx, "AsyncClient", FakeClient)

    with pytest.raises(telegram_bot.TelegramAPIError, match="command list"):
        await telegram_bot.set_bot_commands(bot_token="123456:fake-test-token")


@pytest.mark.asyncio
async def test_set_bot_commands_fails_if_menu_button_is_not_commands(monkeypatch):
    FakeClient.responses = [
        FakeResponse(True),
        FakeResponse(True),
        FakeResponse(telegram_bot.TELEGRAM_CUSTOMER_COMMANDS),
        FakeResponse({"type": "default"}),
    ]
    monkeypatch.setattr(telegram_bot.httpx, "AsyncClient", FakeClient)

    with pytest.raises(telegram_bot.TelegramAPIError, match="menu button"):
        await telegram_bot.set_bot_commands(bot_token="123456:fake-test-token")


def test_contact_request_keyboard_uses_native_request_contact_button():
    keyboard = telegram_bot.contact_request_keyboard()
    button = keyboard["keyboard"][0][0]
    assert button == {"text": "Share My Contact", "request_contact": True}
    assert keyboard["resize_keyboard"] is True
    assert keyboard["one_time_keyboard"] is True
    assert telegram_bot.remove_reply_keyboard() == {"remove_keyboard": True}
