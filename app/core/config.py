import re
from functools import lru_cache
from typing import Literal

from cryptography.fernet import Fernet
from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

LOCAL_SECRET = "local-only-insecure-secret-change-before-deployment"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_env: Literal["local", "test", "staging", "production"] = "local"
    app_name: str = "SmeOps API"
    public_api_base_url: str | None = None
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
    meta_app_id: str | None = None
    meta_app_secret: SecretStr | None = None
    openai_api_key: SecretStr | None = None
    openai_model: str | None = None

    @field_validator(
        "whatsapp_verify_token",
        "whatsapp_app_secret",
        "whatsapp_token_encryption_key",
        "platform_admin_key",
        "openai_api_key",
        "meta_app_secret",
        mode="before",
    )
    @classmethod
    def empty_secrets_are_unset(cls, value: str | None) -> str | None:
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        return value

    @field_validator("database_url")
    @classmethod
    def normalize_database_url(cls, value: str) -> str:
        if value.startswith("postgres://"):
            return value.replace("postgres://", "postgresql+asyncpg://", 1)
        if value.startswith("postgresql://"):
            return value.replace("postgresql://", "postgresql+asyncpg://", 1)
        return value

    @field_validator("app_secret_key")
    @classmethod
    def validate_secret_key(cls, value: SecretStr) -> SecretStr:
        if len(value.get_secret_value()) < 32:
            raise ValueError("APP_SECRET_KEY must contain at least 32 characters")
        return value

    @field_validator("whatsapp_token_encryption_key")
    @classmethod
    def validate_whatsapp_encryption_key(
        cls,
        value: SecretStr | None,
    ) -> SecretStr | None:
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

    @field_validator("meta_app_id")
    @classmethod
    def validate_meta_app_id(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        if not re.fullmatch(r"\d{5,80}", value.strip()):
            raise ValueError("META_APP_ID must be a numeric Meta app ID")
        return value.strip()

    @field_validator("public_api_base_url")
    @classmethod
    def validate_public_api_base_url(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        normalized = value.strip().rstrip("/")
        if not normalized.startswith("https://") or len(normalized) > 512:
            raise ValueError("PUBLIC_API_BASE_URL must be an HTTPS origin or base URL")
        return normalized

    @field_validator("openai_model")
    @classmethod
    def validate_openai_model(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        if len(value) > 120:
            raise ValueError("OPENAI_MODEL must be 120 characters or fewer")
        return value.strip()

    @model_validator(mode="after")
    def reject_local_secret_in_production(self) -> "Settings":
        if self.app_env == "production":
            if self.app_secret_key.get_secret_value() == LOCAL_SECRET:
                raise ValueError("APP_SECRET_KEY must be explicitly configured in production")
            if "localhost" in self.database_url or "127.0.0.1" in self.database_url:
                raise ValueError("DATABASE_URL must point to a managed production database")
            if self.whatsapp_token_encryption_key is None:
                raise ValueError("WHATSAPP_TOKEN_ENCRYPTION_KEY is required in production")
            if self.platform_admin_key is None:
                raise ValueError("PLATFORM_ADMIN_KEY is required in production")
            if self.whatsapp_verify_token is None or self.whatsapp_app_secret is None:
                raise ValueError(
                    "WhatsApp webhook verification secrets are required in production"
                )
            if (
                self.meta_app_id is None
                or self.meta_app_secret is None
                or self.whatsapp_graph_api_version is None
            ):
                raise ValueError(
                    "Meta Embedded Signup credentials are required in production"
                )
        if self.openai_api_key is not None and self.openai_model is None:
            raise ValueError("OPENAI_MODEL is required when OPENAI_API_KEY is configured")
        if (self.meta_app_id is None) != (self.meta_app_secret is None):
            raise ValueError("META_APP_ID and META_APP_SECRET must be configured together")
        if self.meta_app_id is not None and self.whatsapp_graph_api_version is None:
            raise ValueError("WHATSAPP_GRAPH_API_VERSION is required for Meta onboarding")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
