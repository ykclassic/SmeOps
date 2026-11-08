from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, SecretStr


class TelegramConnectionProvision(BaseModel):
    model_config = ConfigDict(hide_input_in_errors=True)

    bot_token: SecretStr = Field(min_length=20, max_length=256)


class TelegramConnectionResponse(BaseModel):
    bot_id: str
    bot_username: str | None
    status: Literal["pending", "active", "disabled"]


class TelegramTextMessageRequest(BaseModel):
    model_config = ConfigDict(hide_input_in_errors=True)

    conversation_id: UUID
    body: str = Field(min_length=1, max_length=4096)
    idempotency_key: UUID


class TelegramOutboundMessageResponse(BaseModel):
    id: UUID
    status: Literal["queued", "sending", "sent", "failed", "unknown"]
