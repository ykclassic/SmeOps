"""Add background worker heartbeat monitoring.

Revision ID: 0012_worker_heartbeats
Revises: 0011_auth_rate_limits
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012_worker_heartbeats"
down_revision: str | None = "0011_auth_rate_limits"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "worker_heartbeats",
        sa.Column("worker_id", sa.String(length=120), nullable=False),
        sa.Column(
            "status",
            sa.String(length=16),
            server_default="running",
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("stopped_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('running', 'stopped')",
            name="ck_worker_heartbeats_worker_heartbeat_status_valid",
        ),
        sa.PrimaryKeyConstraint("worker_id", name="pk_worker_heartbeats"),
    )


def downgrade() -> None:
    op.drop_table("worker_heartbeats")
