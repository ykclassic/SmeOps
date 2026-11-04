"""Add persistent login and registration rate-limit buckets.

Revision ID: 0011_auth_rate_limits
Revises: 0010_whatsapp_embedded_signup
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011_auth_rate_limits"
down_revision: str | None = "0010_whatsapp_embedded_signup"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "auth_rate_limit_buckets",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("key_hash", sa.String(length=64), nullable=False),
        sa.Column("window_started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("blocked_until", sa.DateTime(timezone=True), nullable=True),
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
            "attempts >= 0",
            name="ck_auth_rate_limit_buckets_auth_rate_limit_attempts_nonnegative",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_auth_rate_limit_buckets"),
        sa.UniqueConstraint("key_hash", name="uq_auth_rate_limit_buckets_key_hash"),
    )
    op.create_index(
        "ix_auth_rate_limit_buckets_updated_at",
        "auth_rate_limit_buckets",
        ["updated_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_auth_rate_limit_buckets_updated_at",
        table_name="auth_rate_limit_buckets",
    )
    op.drop_table("auth_rate_limit_buckets")
