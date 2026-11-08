from dataclasses import dataclass

import httpx


@dataclass(frozen=True)
class TelegramSendResult:
    provider_message_id: str


class TelegramSendRejected(RuntimeError):
    """Telegram definitively rejected the outbound message."""


class TelegramDeliveryUnknown(RuntimeError):
    """Telegram may have accepted the request; retrying could duplicate it."""


async def send_text_message(
    *,
    bot_token: str,
    chat_id: str,
    body: str,
) -> TelegramSendResult:
    if not bot_token or len(bot_token) > 256:
        raise ValueError("Invalid Telegram bot token")
    if not chat_id or len(chat_id) > 80:
        raise ValueError("Invalid Telegram chat identifier")
    if not body or len(body) > 4096:
        raise ValueError("Telegram text must contain 1 to 4096 characters")

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    timeout = httpx.Timeout(20.0, connect=5.0)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                url,
                json={"chat_id": chat_id, "text": body},
            )
    except httpx.TransportError as exc:
        raise TelegramDeliveryUnknown("Telegram delivery outcome is unknown") from exc

    if 400 <= response.status_code < 500:
        raise TelegramSendRejected("Telegram rejected the message request")
    if response.status_code >= 500:
        raise TelegramDeliveryUnknown("Telegram server outcome is unknown")

    try:
        data = response.json()
        if data.get("ok") is not True:
            raise TelegramSendRejected("Telegram rejected the message request")
        message_id = data["result"]["message_id"]
    except TelegramSendRejected:
        raise
    except (ValueError, KeyError, TypeError) as exc:
        raise TelegramDeliveryUnknown(
            "Telegram response did not confirm a message ID"
        ) from exc

    if isinstance(message_id, bool) or not isinstance(message_id, (str, int)):
        raise TelegramDeliveryUnknown("Telegram response contained an invalid message ID")
    return TelegramSendResult(provider_message_id=str(message_id))
