"""Add verified Telegram customer identity associations.

Revision ID: 0018_telegram_customer_identity
Revises: 0017_appointment_lifecycle
"""
import sqlalchemy as sa
from alembic import op

revision = "0018_telegram_customer_identity"
down_revision = "0017_appointment_lifecycle"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "telegram_customer_identities",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("customer_id", sa.Uuid(), nullable=False),
        sa.Column("platform_bot_id", sa.Uuid(), nullable=False),
        sa.Column("telegram_user_id", sa.String(length=80), nullable=False),
        sa.Column("telegram_chat_id", sa.String(length=80), nullable=False),
        sa.Column(
            "verified_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"],
            name="fk_telegram_customer_identities_tenant_id_tenants",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "customer_id"],
            ["customers.tenant_id", "customers.id"],
            name="fk_telegram_customer_identities_tenant_customer",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["platform_bot_id"], ["telegram_platform_bots.id"],
            name="fk_telegram_customer_identities_platform_bot_id_telegram_platform_bots",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_telegram_customer_identities"),
        sa.UniqueConstraint(
            "tenant_id", "telegram_user_id",
            name="uq_telegram_customer_identity_tenant_user",
        ),
        sa.UniqueConstraint(
            "tenant_id", "customer_id",
            name="uq_telegram_customer_identity_tenant_customer",
        ),
        sa.UniqueConstraint(
            "tenant_id", "telegram_chat_id",
            name="uq_telegram_customer_identity_tenant_chat",
        ),
    )
    op.create_index(
        "ix_telegram_customer_identity_active",
        "telegram_customer_identities",
        ["tenant_id", "telegram_user_id", "revoked_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_telegram_customer_identity_active",
        table_name="telegram_customer_identities",
    )
    op.drop_table("telegram_customer_identities")
