from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, SecretStr


class WhatsAppConnectionProvision(BaseModel):
    model_config = ConfigDict(hide_input_in_errors=True)

    tenant_id: UUID
    phone_number_id: str = Field(pattern=r"^\d{5,80}$")
    business_account_id: str = Field(pattern=r"^\d{5,80}$")
    access_token: SecretStr = Field(min_length=20, max_length=4096)


class WhatsAppConnectionResponse(BaseModel):
    tenant_id: UUID
    phone_number_id: str
    business_account_id: str
    status: str


class WhatsAppTextMessageRequest(BaseModel):
    model_config = ConfigDict(hide_input_in_errors=True)

    recipient: str = Field(pattern=r"^\+?[1-9]\d{7,14}$")
    body: str = Field(min_length=1, max_length=4096)
    idempotency_key: UUID


class WhatsAppOutboundMessageResponse(BaseModel):
    id: UUID
    status: Literal[
        "queued", "sending", "sent", "delivered", "read", "failed", "unknown"
    ]


class WhatsAppConversationResponse(BaseModel):
    id: UUID
    customer_phone: str
    status: Literal["active", "human_required", "closed"]
    last_inbound_at: datetime
    is_opted_out: bool
    opted_out_at: datetime | None


class WhatsAppConversationMessageResponse(BaseModel):
    id: UUID
    direction: Literal["inbound", "outbound"]
    message_type: str
    body: str | None
    delivery_status: str
    occurred_at: datetime
