"""Add durable WhatsApp outbound message ledger.

Revision ID: 0007_whatsapp_outbound
Revises: 0006_whatsapp_inbox
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007_whatsapp_outbound"
down_revision: str | None = "0006_whatsapp_inbox"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "whatsapp_outbound_messages",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("recipient_encrypted", sa.Text(), nullable=False),
        sa.Column("body_encrypted", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=160), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="queued", nullable=False),
        sa.Column("provider_message_id", sa.String(length=255), nullable=True),
        sa.Column("last_error_code", sa.String(length=80), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'sending', 'sent', 'delivered', 'read', 'failed', 'unknown')",
            name="ck_whatsapp_outbound_messages_whatsapp_outbound_message_status_valid",
        ),
        sa.CheckConstraint(
            "length(idempotency_key) BETWEEN 1 AND 160",
            name="ck_whatsapp_outbound_messages_whatsapp_outbound_message_idempotency_valid",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"],
            name="fk_whatsapp_outbound_messages_tenant_id_tenants",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_whatsapp_outbound_messages"),
        sa.UniqueConstraint(
            "tenant_id", "idempotency_key",
            name="uq_whatsapp_outbound_messages_tenant_idempotency",
        ),
        sa.UniqueConstraint(
            "provider_message_id",
            name="uq_whatsapp_outbound_messages_provider_id",
        ),
    )
    op.create_index(
        "ix_whatsapp_outbound_messages_tenant_created",
        "whatsapp_outbound_messages",
        ["tenant_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_whatsapp_outbound_messages_tenant_created",
        table_name="whatsapp_outbound_messages",
    )
    op.drop_table("whatsapp_outbound_messages")
