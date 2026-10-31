from functools import lru_cache
from typing import Literal

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

    @field_validator("app_secret_key")
    @classmethod
    def validate_secret_key(cls, value: SecretStr) -> SecretStr:
        if len(value.get_secret_value()) < 32:
            raise ValueError("APP_SECRET_KEY must contain at least 32 characters")
        return value

    @model_validator(mode="after")
    def reject_local_secret_in_production(self) -> "Settings":
        if self.app_env == "production" and self.app_secret_key.get_secret_value() == LOCAL_SECRET:
            raise ValueError("APP_SECRET_KEY must be explicitly configured in production")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
