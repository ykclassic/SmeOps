"""Add WhatsApp connections and webhook event inbox.

Revision ID: 0006_whatsapp_inbox
Revises: 0005_background_jobs
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_whatsapp_inbox"
down_revision: str | None = "0005_background_jobs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "whatsapp_connections",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("phone_number_id", sa.String(length=80), nullable=False),
        sa.Column("business_account_id", sa.String(length=80), nullable=False),
        sa.Column("access_token_encrypted", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="active", nullable=False),
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
            "status IN ('active', 'disabled')",
            name="ck_whatsapp_connections_whatsapp_connection_status_valid",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_whatsapp_connections_tenant_id_tenants",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_whatsapp_connections"),
        sa.UniqueConstraint("tenant_id", name="uq_whatsapp_connections_tenant"),
        sa.UniqueConstraint(
            "phone_number_id",
            name="uq_whatsapp_connections_phone_number",
        ),
    )

    op.create_table(
        "whatsapp_webhook_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("provider_event_id", sa.String(length=255), nullable=False),
        sa.Column("event_type", sa.String(length=120), nullable=False),
        sa.Column("payload_encrypted", sa.Text(), nullable=False),
        sa.Column("payload_sha256", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="queued", nullable=False),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('queued', 'accepted', 'processed', 'failed')",
            name="ck_whatsapp_webhook_events_whatsapp_webhook_event_status_valid",
        ),
        sa.CheckConstraint(
            "length(payload_sha256) = 64",
            name="ck_whatsapp_webhook_events_whatsapp_webhook_event_hash_valid",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_whatsapp_webhook_events_tenant_id_tenants",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_whatsapp_webhook_events"),
        sa.UniqueConstraint(
            "tenant_id",
            "provider_event_id",
            name="uq_whatsapp_webhook_events_tenant_provider_event",
        ),
    )
    op.create_index(
        "ix_whatsapp_webhook_events_tenant_received",
        "whatsapp_webhook_events",
        ["tenant_id", "received_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_whatsapp_webhook_events_tenant_received",
        table_name="whatsapp_webhook_events",
    )
    op.drop_table("whatsapp_webhook_events")
    op.drop_table("whatsapp_connections")
