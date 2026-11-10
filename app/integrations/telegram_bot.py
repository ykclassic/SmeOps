from dataclasses import dataclass
from typing import Any

import httpx


@dataclass(frozen=True)
class TelegramBotIdentity:
    bot_id: str
    username: str | None


@dataclass(frozen=True)
class TelegramWebhookInfo:
    url: str
    pending_update_count: int
    last_error_date: int | None
    last_error_message: str | None


@dataclass(frozen=True)
class TelegramSendResult:
    provider_message_id: str


class TelegramSendRejected(RuntimeError):
    """Telegram definitively rejected the outbound message."""


class TelegramDeliveryUnknown(RuntimeError):
    """Telegram may have accepted the request; retrying could duplicate it."""


class TelegramAPIError(RuntimeError):
    """Telegram rejected a provisioning/API operation."""


def _validate_token(bot_token: str) -> None:
    if not bot_token or len(bot_token) > 256 or ":" not in bot_token:
        raise ValueError("Invalid Telegram bot token")


async def _call_bot_api(
    *,
    bot_token: str,
    method: str,
    payload: dict[str, Any],
) -> Any:
    _validate_token(bot_token)
    url = f"https://api.telegram.org/bot{bot_token}/{method}"
    timeout = httpx.Timeout(20.0, connect=5.0)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(url, json=payload)
    except httpx.TransportError:
        raise TelegramDeliveryUnknown("Telegram API outcome is unknown") from None

    try:
        data = response.json()
    except (ValueError, TypeError):
        if response.status_code >= 500 or response.status_code < 200:
            raise TelegramDeliveryUnknown("Telegram API outcome is unknown") from None
        raise TelegramAPIError("Telegram returned an invalid API response") from None

    if response.status_code >= 500 or response.status_code < 200:
        raise TelegramDeliveryUnknown("Telegram API outcome is unknown")
    if not isinstance(data, dict) or data.get("ok") is not True:
        raise TelegramAPIError("Telegram API request was rejected")
    result = data.get("result")
    if isinstance(result, (dict, list)):
        return result
    if result is True:
        return {}
    raise TelegramDeliveryUnknown("Telegram API response did not contain a result")


async def get_bot_identity(*, bot_token: str) -> TelegramBotIdentity:
    result = await _call_bot_api(bot_token=bot_token, method="getMe", payload={})
    bot_id = result.get("id")
    username = result.get("username")
    if isinstance(bot_id, bool) or not isinstance(bot_id, int) or bot_id <= 0:
        raise TelegramAPIError("Telegram getMe returned an invalid bot identity")
    if username is not None and (
        not isinstance(username, str) or len(username) > 80
    ):
        raise TelegramAPIError("Telegram getMe returned an invalid bot username")
    return TelegramBotIdentity(bot_id=str(bot_id), username=username)


async def get_webhook_info(*, bot_token: str) -> TelegramWebhookInfo:
    result = await _call_bot_api(
        bot_token=bot_token,
        method="getWebhookInfo",
        payload={},
    )
    url = result.get("url", "")
    pending = result.get("pending_update_count", 0)
    last_error_date = result.get("last_error_date")
    last_error_message = result.get("last_error_message")
    if (
        not isinstance(url, str)
        or isinstance(pending, bool)
        or not isinstance(pending, int)
        or pending < 0
    ):
        raise TelegramAPIError("Telegram returned invalid webhook diagnostics")
    if last_error_date is not None and (
        isinstance(last_error_date, bool) or not isinstance(last_error_date, int)
    ):
        raise TelegramAPIError("Telegram returned invalid webhook error timestamp")
    if last_error_message is not None and not isinstance(last_error_message, str):
        raise TelegramAPIError("Telegram returned invalid webhook error diagnostics")
    return TelegramWebhookInfo(
        url=url,
        pending_update_count=pending,
        last_error_date=last_error_date,
        last_error_message=last_error_message,
    )


async def register_webhook(
    *,
    bot_token: str,
    webhook_url: str,
    secret_token: str,
) -> None:
    if not webhook_url.startswith("https://") or len(webhook_url) > 2048:
        raise ValueError("Telegram webhook URL must be a valid HTTPS URL")
    if not 1 <= len(secret_token) <= 256:
        raise ValueError("Invalid Telegram webhook secret")
    allowed = set(
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-"
    )
    if any(character not in allowed for character in secret_token):
        raise ValueError("Invalid Telegram webhook secret characters")
    await _call_bot_api(
        bot_token=bot_token,
        method="setWebhook",
        payload={
            "url": webhook_url,
            "secret_token": secret_token,
            "allowed_updates": ["message"],
            "drop_pending_updates": False,
        },
    )


async def remove_webhook(*, bot_token: str) -> None:
    await _call_bot_api(
        bot_token=bot_token,
        method="deleteWebhook",
        payload={"drop_pending_updates": False},
    )


TELEGRAM_CUSTOMER_COMMANDS = [
    {"command": "book", "description": "Book an appointment"},
    {"command": "appointments", "description": "View your appointments"},
    {"command": "cancel", "description": "Cancel an appointment"},
    {"command": "reschedule", "description": "Change an appointment time"},
    {"command": "help", "description": "Show help and options"},
]
TELEGRAM_PRIVATE_COMMAND_SCOPE = {"type": "all_private_chats"}
TELEGRAM_COMMANDS_MENU_BUTTON = {"type": "commands"}


_configured_private_chat_commands: set[tuple[str, str]] = set()


async def ensure_private_chat_commands(
    *,
    bot_token: str,
    bot_id: str,
    chat_id: str,
    force: bool = False,
) -> None:
    """Apply and verify commands/menu at the exact private-chat scope.

    Chat-scoped Telegram settings override all_private_chats settings, so
    onboarding must explicitly provision each customer/owner private chat.
    """
    if not chat_id or not chat_id.isdigit() or len(chat_id) > 80:
        raise ValueError("Invalid Telegram private chat identifier")
    cache_key = (bot_id, chat_id)
    if not force and cache_key in _configured_private_chat_commands:
        return

    scope = {"type": "chat", "chat_id": int(chat_id)}
    menu_payload = {
        "chat_id": int(chat_id),
        "menu_button": TELEGRAM_COMMANDS_MENU_BUTTON,
    }
    await _call_bot_api(
        bot_token=bot_token,
        method="setMyCommands",
        payload={"commands": TELEGRAM_CUSTOMER_COMMANDS, "scope": scope},
    )
    await _call_bot_api(
        bot_token=bot_token,
        method="setChatMenuButton",
        payload=menu_payload,
    )

    registered = await _call_bot_api(
        bot_token=bot_token,
        method="getMyCommands",
        payload={"scope": scope},
    )
    if registered != TELEGRAM_CUSTOMER_COMMANDS:
        raise TelegramAPIError(
            "Telegram did not confirm the expected commands for this private chat"
        )

    menu_button = await _call_bot_api(
        bot_token=bot_token,
        method="getChatMenuButton",
        payload={"chat_id": int(chat_id)},
    )
    if menu_button.get("type") != "commands":
        raise TelegramAPIError(
            "Telegram did not confirm the commands menu for this private chat"
        )
    _configured_private_chat_commands.add(cache_key)


async def set_bot_commands(*, bot_token: str) -> None:
    """Configure and verify the command list and menu button for private chats."""
    scope = TELEGRAM_PRIVATE_COMMAND_SCOPE
    await _call_bot_api(
        bot_token=bot_token,
        method="setMyCommands",
        payload={"commands": TELEGRAM_CUSTOMER_COMMANDS, "scope": scope},
    )
    await _call_bot_api(
        bot_token=bot_token,
        method="setChatMenuButton",
        payload={"menu_button": TELEGRAM_COMMANDS_MENU_BUTTON},
    )
    registered = await _call_bot_api(
        bot_token=bot_token,
        method="getMyCommands",
        payload={"scope": scope},
    )
    if registered != TELEGRAM_CUSTOMER_COMMANDS:
        raise TelegramAPIError(
            "Telegram did not confirm the expected private-chat command list"
        )
    menu_button = await _call_bot_api(
        bot_token=bot_token,
        method="getChatMenuButton",
        payload={},
    )
    if menu_button.get("type") != "commands":
        raise TelegramAPIError("Telegram did not confirm the commands menu button")


def contact_request_keyboard() -> dict[str, Any]:
    return {
        "keyboard": [
            [{"text": "Share My Contact", "request_contact": True}],
        ],
        "resize_keyboard": True,
        "one_time_keyboard": True,
        "input_field_placeholder": "Tap Share My Contact",
    }


def remove_reply_keyboard() -> dict[str, bool]:
    return {"remove_keyboard": True}


async def send_text_message(
    *,
    bot_token: str,
    chat_id: str,
    body: str,
    reply_markup: dict[str, Any] | None = None,
) -> TelegramSendResult:
    if not chat_id or len(chat_id) > 80:
        raise ValueError("Invalid Telegram chat identifier")
    if not body or len(body) > 4096:
        raise ValueError("Telegram text must contain 1 to 4096 characters")
    try:
        payload = {"chat_id": chat_id, "text": body}
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        result = await _call_bot_api(
            bot_token=bot_token,
            method="sendMessage",
            payload=payload,
        )
    except TelegramAPIError as exc:
        raise TelegramSendRejected("Telegram rejected the message request") from exc
    message_id = result.get("message_id")
    if isinstance(message_id, bool) or not isinstance(message_id, (str, int)):
        raise TelegramDeliveryUnknown("Telegram response contained an invalid message ID")
    return TelegramSendResult(provider_message_id=str(message_id))
