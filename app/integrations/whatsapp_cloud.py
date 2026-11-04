from dataclasses import dataclass

import httpx


@dataclass(frozen=True)
class WhatsAppSendResult:
    provider_message_id: str


class WhatsAppSendRejected(RuntimeError):
    """The provider definitively rejected the outbound message."""


class WhatsAppDeliveryUnknown(RuntimeError):
    """The request may have reached the provider; automatic retry is unsafe."""


async def send_text_message(
    *,
    api_version: str,
    phone_number_id: str,
    access_token: str,
    recipient: str,
    body: str,
) -> WhatsAppSendResult:
    url = f"https://graph.facebook.com/{api_version}/{phone_number_id}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": recipient,
        "type": "text",
        "text": {"preview_url": False, "body": body},
    }

    timeout = httpx.Timeout(20.0, connect=5.0)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                url,
                headers={"Authorization": f"Bearer {access_token}"},
                json=payload,
            )
    except httpx.TransportError as exc:
        raise WhatsAppDeliveryUnknown("WhatsApp transport outcome is unknown") from exc

    if 400 <= response.status_code < 500:
        raise WhatsAppSendRejected("WhatsApp rejected the message request")
    if response.status_code >= 500:
        raise WhatsAppDeliveryUnknown("WhatsApp server outcome is unknown")

    try:
        data = response.json()
        messages = data["messages"]
        message_id = messages[0]["id"]
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise WhatsAppDeliveryUnknown("WhatsApp response did not confirm a message ID") from exc

    if not isinstance(message_id, str) or not message_id:
        raise WhatsAppDeliveryUnknown("WhatsApp response contained an invalid message ID")
    return WhatsAppSendResult(provider_message_id=message_id)
