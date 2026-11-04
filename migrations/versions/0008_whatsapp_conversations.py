"""Add tenant-scoped WhatsApp conversations and inbound message history.

Revision ID: 0008_whatsapp_conversations
Revises: 0007_whatsapp_outbound
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008_whatsapp_conversations"
down_revision: str | None = "0007_whatsapp_outbound"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "whatsapp_conversations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("customer_phone_hash", sa.String(length=64), nullable=False),
        sa.Column("customer_phone_encrypted", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=24), server_default="active", nullable=False),
        sa.Column("context", sa.JSON(), server_default=sa.text("'{}'"), nullable=False),
        sa.Column("last_inbound_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "status IN ('active', 'human_required', 'closed')",
            name="ck_whatsapp_conversations_whatsapp_conversation_status_valid",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"],
            name="fk_whatsapp_conversations_tenant_id_tenants",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_whatsapp_conversations"),
        sa.UniqueConstraint(
            "tenant_id", "id", name="uq_whatsapp_conversations_tenant_id_id"
        ),
        sa.UniqueConstraint(
            "tenant_id", "customer_phone_hash",
            name="uq_whatsapp_conversations_tenant_phone",
        ),
    )
    op.create_index(
        "ix_whatsapp_conversations_tenant_updated",
        "whatsapp_conversations",
        ["tenant_id", "updated_at"],
    )

    op.create_table(
        "whatsapp_conversation_messages",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("provider_message_id", sa.String(length=255), nullable=False),
        sa.Column("direction", sa.String(length=12), nullable=False),
        sa.Column("message_type", sa.String(length=40), nullable=False),
        sa.Column("body_encrypted", sa.Text(), nullable=True),
        sa.Column("provider_timestamp", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delivery_status", sa.String(length=16), server_default="received", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "direction IN ('inbound', 'outbound')",
            name="ck_whatsapp_conversation_messages_whatsapp_conversation_message_direction_valid",
        ),
        sa.CheckConstraint(
            "length(provider_message_id) BETWEEN 1 AND 255",
            name="ck_whatsapp_conversation_messages_whatsapp_conversation_message_provider_id_valid",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "conversation_id"],
            ["whatsapp_conversations.tenant_id", "whatsapp_conversations.id"],
            name="fk_whatsapp_conversation_messages_tenant_conversation",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"],
            name="fk_whatsapp_conversation_messages_tenant_id_tenants",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_whatsapp_conversation_messages"),
        sa.UniqueConstraint(
            "tenant_id", "provider_message_id",
            name="uq_whatsapp_conversation_messages_tenant_provider_id",
        ),
    )
    op.create_index(
        "ix_whatsapp_conversation_messages_tenant_conversation_created",
        "whatsapp_conversation_messages",
        ["tenant_id", "conversation_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_whatsapp_conversation_messages_tenant_conversation_created",
        table_name="whatsapp_conversation_messages",
    )
    op.drop_table("whatsapp_conversation_messages")
    op.drop_index(
        "ix_whatsapp_conversations_tenant_updated",
        table_name="whatsapp_conversations",
    )
    op.drop_table("whatsapp_conversations")
