import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr, ValidationError

from app.core.config import Settings


def _production_settings(**overrides):
    values = {
        "app_env": "production",
        "app_secret_key": SecretStr("production-secret-that-is-at-least-32-chars"),
        "database_url": "postgresql+asyncpg://smeops:secret@db.internal:5432/smeops",
        "whatsapp_verify_token": SecretStr("meta-webhook-verify-token"),
        "whatsapp_app_secret": SecretStr("meta-webhook-app-secret"),
        "whatsapp_token_encryption_key": SecretStr(Fernet.generate_key().decode()),
        "whatsapp_graph_api_version": "v26.0",
        "meta_app_id": "123456789012345",
        "meta_app_secret": SecretStr("meta-application-secret"),
        "platform_admin_key": None,
        "_env_file": None,
    }
    values.update(overrides)
    return Settings(**values)


def test_production_configuration_requires_platform_operations_key():
    with pytest.raises(ValidationError, match="PLATFORM_ADMIN_KEY"):
        _production_settings()

    configured = _production_settings(
        platform_admin_key=SecretStr("high-entropy-platform-operations-secret")
    )
    assert configured.app_env == "production"
    assert configured.platform_admin_key is not None
