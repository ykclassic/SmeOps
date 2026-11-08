"""Add tenant-scoped Telegram channel persistence.

Revision ID: 0013_telegram_channel
Revises: 0012_worker_heartbeats
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013_telegram_channel"
down_revision: str | None = "0012_worker_heartbeats"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "telegram_connections",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("bot_id", sa.String(80), nullable=False),
        sa.Column("bot_username", sa.String(80), nullable=True),
        sa.Column("bot_token_encrypted", sa.Text(), nullable=False),
        sa.Column("webhook_secret_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), server_default="pending", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'active', 'disabled')",
            name="ck_telegram_connections_telegram_connection_status_valid",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name="pk_telegram_connections"),
        sa.UniqueConstraint("tenant_id", name="uq_telegram_connections_tenant"),
        sa.UniqueConstraint("bot_id", name="uq_telegram_connections_bot_id"),
    )
    op.create_table(
        "telegram_webhook_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("provider_update_id", sa.String(80), nullable=False),
        sa.Column("event_type", sa.String(80), nullable=False),
        sa.Column("payload_encrypted", sa.Text(), nullable=False),
        sa.Column("payload_sha256", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), server_default="queued", nullable=False),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('queued', 'processed', 'failed')",
            name="ck_telegram_webhook_events_telegram_webhook_event_status_valid",
        ),
        sa.CheckConstraint(
            "length(payload_sha256) = 64",
            name="ck_telegram_webhook_events_telegram_webhook_event_hash_valid",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name="pk_telegram_webhook_events"),
        sa.UniqueConstraint(
            "tenant_id",
            "provider_update_id",
            name="uq_telegram_webhook_events_tenant_update",
        ),
    )
    op.create_index(
        "ix_telegram_webhook_events_tenant_received",
        "telegram_webhook_events",
        ["tenant_id", "received_at"],
    )
    op.create_table(
        "telegram_conversations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("telegram_user_id", sa.String(80), nullable=False),
        sa.Column("telegram_chat_id", sa.String(80), nullable=False),
        sa.Column("customer_name", sa.String(160), nullable=True),
        sa.Column("customer_phone_encrypted", sa.Text(), nullable=True),
        sa.Column("status", sa.String(24), server_default="active", nullable=False),
        sa.Column("context", sa.JSON(), server_default=sa.text("'{}'"), nullable=False),
        sa.Column(
            "last_inbound_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('active', 'human_required', 'closed')",
            name="ck_telegram_conversations_telegram_conversation_status_valid",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name="pk_telegram_conversations"),
        sa.UniqueConstraint(
            "tenant_id",
            "id",
            name="uq_telegram_conversations_tenant_id_id",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "telegram_chat_id",
            name="uq_telegram_conversations_tenant_chat",
        ),
    )
    op.create_index(
        "ix_telegram_conversations_tenant_updated",
        "telegram_conversations",
        ["tenant_id", "updated_at"],
    )
    op.create_table(
        "telegram_conversation_messages",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("provider_message_id", sa.String(80), nullable=False),
        sa.Column("direction", sa.String(12), nullable=False),
        sa.Column("body_encrypted", sa.Text(), nullable=True),
        sa.Column("delivery_status", sa.String(16), server_default="received", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "direction IN ('inbound', 'outbound')",
            name="ck_telegram_conversation_messages_telegram_message_direction_valid",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "conversation_id"],
            ["telegram_conversations.tenant_id", "telegram_conversations.id"],
            name="fk_telegram_messages_tenant_conversation",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_telegram_conversation_messages"),
        sa.UniqueConstraint(
            "tenant_id",
            "provider_message_id",
            name="uq_telegram_messages_tenant_provider_id",
        ),
    )
    op.create_index(
        "ix_telegram_messages_tenant_conversation_created",
        "telegram_conversation_messages",
        ["tenant_id", "conversation_id", "created_at"],
    )
    op.create_table(
        "telegram_outbound_messages",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("recipient_chat_id", sa.String(80), nullable=False),
        sa.Column("body_encrypted", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.String(160), nullable=False),
        sa.Column("status", sa.String(16), server_default="queued", nullable=False),
        sa.Column("provider_message_id", sa.String(80), nullable=True),
        sa.Column("last_error_code", sa.String(80), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'sending', 'sent', 'failed', 'unknown')",
            name="ck_telegram_outbound_messages_telegram_outbound_status_valid",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "conversation_id"],
            ["telegram_conversations.tenant_id", "telegram_conversations.id"],
            name="fk_telegram_outbound_messages_tenant_id_telegram_conversations",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_telegram_outbound_messages"),
        sa.UniqueConstraint(
            "tenant_id",
            "idempotency_key",
            name="uq_telegram_outbound_tenant_idempotency",
        ),
    )
    op.create_index(
        "ix_telegram_outbound_tenant_created",
        "telegram_outbound_messages",
        ["tenant_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_telegram_outbound_tenant_created",
        table_name="telegram_outbound_messages",
    )
    op.drop_table("telegram_outbound_messages")
    op.drop_index(
        "ix_telegram_messages_tenant_conversation_created",
        table_name="telegram_conversation_messages",
    )
    op.drop_table("telegram_conversation_messages")
    op.drop_index(
        "ix_telegram_conversations_tenant_updated",
        table_name="telegram_conversations",
    )
    op.drop_table("telegram_conversations")
    op.drop_index(
        "ix_telegram_webhook_events_tenant_received",
        table_name="telegram_webhook_events",
    )
    op.drop_table("telegram_webhook_events")
    op.drop_table("telegram_connections")
