from uuid import UUID

from pydantic import BaseModel, Field


class WhatsAppConnectionProvision(BaseModel):
    tenant_id: UUID
    phone_number_id: str = Field(pattern=r"^\d{5,80}$")
    business_account_id: str = Field(pattern=r"^\d{5,80}$")
    access_token: str = Field(min_length=20, max_length=4096)


class WhatsAppConnectionResponse(BaseModel):
    tenant_id: UUID
    phone_number_id: str
    business_account_id: str
    status: str
