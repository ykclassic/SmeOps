import json
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict, ValidationError

from app.core.config import get_settings


class ConversationIntent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    intent: Literal[
        "booking_start",
        "human_request",
        "affirmative",
        "negative",
        "other",
    ]
    confidence: float


class ConversationAIUnavailable(RuntimeError):
    """The configured conversation interpreter could not be reached or parsed."""


_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "intent": {
            "type": "string",
            "enum": [
                "booking_start",
                "human_request",
                "affirmative",
                "negative",
                "other",
            ],
        },
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
    "required": ["intent", "confidence"],
    "additionalProperties": False,
}


async def classify_inbound_intent(message: str) -> ConversationIntent | None:
    settings = get_settings()
    if settings.openai_api_key is None or settings.openai_model is None:
        return None

    request_body = {
        "model": settings.openai_model,
        "instructions": (
            "Classify the customer's latest message for a salon appointment assistant. "
            "Return only the requested structured fields. Do not invent facts or actions. "
            "booking_start means the customer wants to make an appointment. "
            "human_request means they want a person. affirmative and negative are clear "
            "yes/no responses. All other messages are other."
        ),
        "input": message[:2000],
        "text": {
            "format": {
                "type": "json_schema",
                "name": "smeops_message_intent",
                "strict": True,
                "schema": _RESPONSE_SCHEMA,
            }
        },
        "max_output_tokens": 100,
        "store": False,
    }
    headers = {
        "Authorization": f"Bearer {settings.openai_api_key.get_secret_value()}",
        "Content-Type": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(8.0, connect=3.0)) as client:
            response = await client.post(
                "https://api.openai.com/v1/responses",
                headers=headers,
                json=request_body,
            )
            response.raise_for_status()
            payload = response.json()
        output_text = payload.get("output_text")
        if not isinstance(output_text, str):
            output_items = payload.get("output")
            text_parts = []
            if isinstance(output_items, list):
                for item in output_items:
                    if not isinstance(item, dict) or item.get("type") != "message":
                        continue
                    for content in item.get("content", []):
                        if (
                            isinstance(content, dict)
                            and content.get("type") == "output_text"
                            and isinstance(content.get("text"), str)
                        ):
                            text_parts.append(content["text"])
            output_text = "".join(text_parts)
        if not output_text:
            raise ValueError("Model response did not contain structured output")
        return ConversationIntent.model_validate(json.loads(output_text))
    except (
        httpx.HTTPError,
        ValueError,
        TypeError,
        KeyError,
        ValidationError,
    ) as exc:
        raise ConversationAIUnavailable("Conversation interpreter unavailable") from exc
