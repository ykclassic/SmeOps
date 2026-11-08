"""Align shared Telegram webhook secret hash with runtime derivation.

Revision ID: 0016_telegram_platform_webhook_secret
Revises: 0015_telegram_platform_bot
"""
import hashlib
import hmac

import sqlalchemy as sa
from alembic import op

from app.core.config import get_settings

revision = "0016_telegram_platform_webhook_secret"
down_revision = "0015_telegram_platform_bot"
branch_labels = None
depends_on = None


def upgrade() -> None:
    settings = get_settings()
    if "app_secret_key" not in settings.model_fields_set:
        raise RuntimeError(
            "APP_SECRET_KEY must be explicitly configured before Telegram webhook migration"
        )
    bot_id = "9016622451"
    key = settings.app_secret_key.get_secret_value().encode()
    secret = hmac.new(
        key,
        f"smeops:telegram:webhook:{bot_id}".encode(),
        hashlib.sha256,
    ).hexdigest()
    secret_hash = hashlib.sha256(secret.encode()).hexdigest()
    bind = op.get_bind()
    bind.execute(
        sa.text(
            "UPDATE telegram_platform_bots "
            "SET webhook_secret_hash = :secret_hash, updated_at = now() "
            "WHERE singleton_key = 'platform' AND bot_id = :bot_id"
        ),
        {"secret_hash": secret_hash, "bot_id": bot_id},
    )


def downgrade() -> None:
    # The prior hash was copied from a legacy tenant connection and cannot be
    # reconstructed safely. Keep the secure runtime-derived hash on downgrade.
    pass
