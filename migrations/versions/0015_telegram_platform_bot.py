"""Introduce shared platform Telegram bot and tenant booking references.

Revision ID: 0015_telegram_platform_bot
Revises: 0014_telegram_owner_alerts

The migration copies the existing known platform bot credential only when exactly
one active legacy connection exists and its bot identity is the established
SmeOps bot. It never calls Telegram or prints credential material.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015_telegram_platform_bot"
down_revision: str | None = "0014_telegram_owner_alerts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

EXPECTED_BOT_ID = "901662451"


def upgrade() -> None:
    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            "SELECT bot_id, bot_username, bot_token_encrypted, webhook_secret_hash "
            "FROM telegram_connections WHERE status = 'active'"
        )
    ).mappings().all()
    if len(rows) != 1 or rows[0]["bot_id"] != EXPECTED_BOT_ID:
        raise RuntimeError(
            "Telegram platform-bot promotion requires exactly one active legacy "
            "connection matching the established SmeOps bot identity."
        )

    row = rows[0]
    op.create_table(
        "telegram_platform_bots",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("singleton_key", sa.String(16), server_default="platform", nullable=False),
        sa.Column("bot_id", sa.String(80), nullable=False),
        sa.Column("bot_username", sa.String(80), nullable=False),
        sa.Column("bot_token_encrypted", sa.Text(), nullable=False),
        sa.Column("webhook_secret_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), server_default="active", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("singleton_key = 'platform'", name="ck_telegram_platform_bots_telegram_platform_bot_singleton"),
        sa.CheckConstraint("status IN ('active', 'disabled')", name="ck_telegram_platform_bots_telegram_platform_bot_status"),
        sa.PrimaryKeyConstraint("id", name="pk_telegram_platform_bots"),
        sa.UniqueConstraint("singleton_key", name="uq_telegram_platform_bots_singleton_key"),
        sa.UniqueConstraint("bot_id", name="uq_telegram_platform_bots_bot_id"),
    )
    bind.execute(
        sa.text(
            "INSERT INTO telegram_platform_bots "
            "(id, singleton_key, bot_id, bot_username, bot_token_encrypted, "
            "webhook_secret_hash, status) "
            "VALUES (:id, 'platform', :bot_id, :username, :token, :secret_hash, 'active')"
        ),
        {
            "id": "00000000-0000-0000-0000-000000000015",
            "bot_id": row["bot_id"],
            "username": row["bot_username"] or "smeops_bot",
            "token": row["bot_token_encrypted"],
            "secret_hash": row["webhook_secret_hash"],
        },
    )

    op.add_column("telegram_webhook_events", sa.Column("bot_id", sa.String(80), nullable=True))
    bind.execute(
        sa.text(
            "UPDATE telegram_webhook_events AS event SET bot_id = connection.bot_id "
            "FROM telegram_connections AS connection WHERE connection.tenant_id = event.tenant_id"
        )
    )
    op.alter_column("telegram_webhook_events", "bot_id", nullable=False)
    op.create_unique_constraint(
        "uq_telegram_webhook_events_bot_update", "telegram_webhook_events", ["bot_id", "provider_update_id"]
    )

    op.create_table(
        "telegram_booking_references",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("token_encrypted", sa.Text(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name="pk_telegram_booking_references"),
        sa.UniqueConstraint("token_hash", name="uq_telegram_booking_references_token_hash"),
        sa.UniqueConstraint("tenant_id", name="uq_telegram_booking_references_tenant"),
    )
    op.create_index(
        "ix_telegram_booking_references_tenant_active",
        "telegram_booking_references", ["tenant_id", "revoked_at"],
    )

    op.create_table(
        "telegram_chat_contexts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("platform_bot_id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("telegram_user_id", sa.String(80), nullable=False),
        sa.Column("telegram_chat_id", sa.String(80), nullable=False),
        sa.Column("selected_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["platform_bot_id"], ["telegram_platform_bots.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name="pk_telegram_chat_contexts"),
        sa.UniqueConstraint("platform_bot_id", "telegram_chat_id", name="uq_telegram_chat_contexts_bot_chat"),
    )
    op.create_index("ix_telegram_chat_contexts_tenant", "telegram_chat_contexts", ["tenant_id"])


def downgrade() -> None:
    op.drop_constraint("uq_telegram_webhook_events_bot_update", "telegram_webhook_events", type_="unique")
    op.drop_column("telegram_webhook_events", "bot_id")
    op.drop_index("ix_telegram_chat_contexts_tenant", table_name="telegram_chat_contexts")
    op.drop_table("telegram_chat_contexts")
    op.drop_index("ix_telegram_booking_references_tenant_active", table_name="telegram_booking_references")
    op.drop_table("telegram_booking_references")
    op.drop_table("telegram_platform_bots")
