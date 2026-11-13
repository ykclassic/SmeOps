from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, SecretStr


class OwnerDeactivationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    confirmation: Literal["DEACTIVATE"]
    password: SecretStr = Field(min_length=12, max_length=128)
    reason: str | None = Field(default=None, max_length=500)


class OwnerDeactivationResponse(BaseModel):
    status: Literal["deactivated"]
    changed: bool
    tenant_id: UUID


class OwnerReactivationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    confirmation: Literal["REACTIVATE"]
    password: SecretStr = Field(min_length=12, max_length=128)
    reason: str | None = Field(default=None, max_length=500)


class OwnerReactivationResponse(BaseModel):
    status: Literal["active", "deactivated"]
    changed: bool
    tenant_id: UUID


class OwnerLifecycleStatusResponse(BaseModel):
    status: Literal["active", "deactivated"]
    tenant_id: UUID
