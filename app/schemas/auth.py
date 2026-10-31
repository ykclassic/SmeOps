from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, EmailStr, Field, SecretStr, field_validator


class RegisterRequest(BaseModel):
    model_config = ConfigDict(hide_input_in_errors=True)

    business_name: str = Field(min_length=2, max_length=160)
    tenant_slug: str = Field(
        pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$",
        min_length=3,
        max_length=80,
    )
    email: EmailStr
    password: SecretStr = Field(min_length=12, max_length=128)
    business_timezone: str = Field(default="Africa/Lagos", min_length=1, max_length=64)

    @field_validator("business_name")
    @classmethod
    def normalize_business_name(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if len(normalized) < 2:
            raise ValueError("Business name must contain at least two non-space characters")
        return normalized

    @field_validator("business_timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("Business timezone must be a valid IANA timezone") from exc
        return value


class LoginRequest(BaseModel):
    model_config = ConfigDict(hide_input_in_errors=True)

    tenant_slug: str = Field(min_length=3, max_length=80)
    email: EmailStr
    password: SecretStr = Field(min_length=1, max_length=128)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class CurrentUserResponse(BaseModel):
    id: str
    tenant_id: str
    email: EmailStr
    role: str
