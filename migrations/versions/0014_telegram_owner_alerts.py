"""Add Telegram owner linking and appointment notifications.

Revision ID: 0014_telegram_owner_alerts
Revises: 0013_telegram_channel
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014_telegram_owner_alerts"
down_revision: str | None = "0013_telegram_channel"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "telegram_owner_links",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("owner_user_id", sa.Uuid(), nullable=False),
        sa.Column("invitation_hash", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("chat_id", sa.String(80), nullable=True),
        sa.Column("linked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("disabled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "owner_user_id"], ["users.tenant_id", "users.id"],
            name="fk_telegram_owner_links_tenant_owner", ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_telegram_owner_links"),
        sa.UniqueConstraint("tenant_id", "owner_user_id", name="uq_telegram_owner_links_owner"),
        sa.UniqueConstraint("tenant_id", "chat_id", name="uq_telegram_owner_links_chat"),
        sa.UniqueConstraint("invitation_hash", name="uq_telegram_owner_links_invitation_hash"),
    )
    op.create_index(
        "ix_telegram_owner_links_tenant_linked", "telegram_owner_links",
        ["tenant_id", "linked_at"],
    )
    op.create_table(
        "telegram_owner_notifications",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("owner_user_id", sa.Uuid(), nullable=False),
        sa.Column("appointment_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(16), server_default="queued", nullable=False),
        sa.Column("provider_message_id", sa.String(80), nullable=True),
        sa.Column("last_error_code", sa.String(80), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "status IN ('queued', 'sending', 'sent', 'failed', 'unknown')",
            name="ck_telegram_owner_notifications_telegram_owner_notification_status_valid",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["appointment_id"], ["appointments.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "owner_user_id"], ["users.tenant_id", "users.id"],
            name="fk_telegram_owner_notifications_tenant_owner", ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_telegram_owner_notifications"),
        sa.UniqueConstraint(
            "tenant_id", "owner_user_id", "appointment_id",
            name="uq_telegram_owner_notifications_appointment_owner",
        ),
    )
    op.create_index(
        "ix_telegram_owner_notifications_tenant_created",
        "telegram_owner_notifications", ["tenant_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_telegram_owner_notifications_tenant_created", table_name="telegram_owner_notifications")
    op.drop_table("telegram_owner_notifications")
    op.drop_index("ix_telegram_owner_links_tenant_linked", table_name="telegram_owner_links")
    op.drop_table("telegram_owner_links")
