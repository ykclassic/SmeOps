import re
from functools import lru_cache
from typing import Literal

from cryptography.fernet import Fernet
from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

LOCAL_SECRET = "local-only-insecure-secret-change-before-deployment"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: Literal["local", "test", "staging", "production"] = "local"
    app_name: str = "SmeOps API"
    app_secret_key: SecretStr = SecretStr(LOCAL_SECRET)
    database_url: str = "postgresql+asyncpg://smeops:smeops@localhost:5432/smeops"
    log_level: str = "INFO"
    jwt_issuer: str = "smeops-api"
    access_token_ttl_minutes: int = Field(default=30, ge=5, le=120)
    whatsapp_verify_token: SecretStr | None = None
    whatsapp_app_secret: SecretStr | None = None
    whatsapp_token_encryption_key: SecretStr | None = None
    platform_admin_key: SecretStr | None = None
    whatsapp_graph_api_version: str | None = None

    @field_validator(
        "whatsapp_verify_token",
        "whatsapp_app_secret",
        "whatsapp_token_encryption_key",
        "platform_admin_key",
        mode="before",
    )
    @classmethod
    def empty_secrets_are_unset(cls, value: str | None) -> str | None:
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        return value

    @field_validator("app_secret_key")
    @classmethod
    def validate_secret_key(cls, value: SecretStr) -> SecretStr:
        if len(value.get_secret_value()) < 32:
            raise ValueError("APP_SECRET_KEY must contain at least 32 characters")
        return value

    @field_validator("whatsapp_token_encryption_key")
    @classmethod
    def validate_whatsapp_encryption_key(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None:
            return None
        try:
            Fernet(value.get_secret_value().encode())
        except (ValueError, TypeError) as exc:
            raise ValueError("WHATSAPP_TOKEN_ENCRYPTION_KEY must be a valid Fernet key") from exc
        return value

    @field_validator("whatsapp_graph_api_version")
    @classmethod
    def validate_graph_api_version(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        if not re.fullmatch(r"v\d+\.\d+", value):
            raise ValueError("WHATSAPP_GRAPH_API_VERSION must use the form vNN.N")
        return value

    @model_validator(mode="after")
    def reject_local_secret_in_production(self) -> "Settings":
        if self.app_env == "production" and self.app_secret_key.get_secret_value() == LOCAL_SECRET:
            raise ValueError("APP_SECRET_KEY must be explicitly configured in production")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
