"""Add durable background job queue.

Revision ID: 0005_background_jobs
Revises: 0004_retail_orders
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_background_jobs"
down_revision: str | None = "0004_retail_orders"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "background_jobs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("job_type", sa.String(length=80), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=160), nullable=False),
        sa.Column("payload_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="queued", nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("max_attempts", sa.Integer(), server_default="3", nullable=False),
        sa.Column(
            "run_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("locked_by", sa.String(length=120), nullable=True),
        sa.Column("last_error_code", sa.String(length=80), nullable=True),
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
            "status IN ('queued', 'running', 'completed', 'dead_letter')",
            name="ck_background_jobs_background_job_status_valid",
        ),
        sa.CheckConstraint(
            "attempts >= 0 AND max_attempts BETWEEN 1 AND 10 AND attempts <= max_attempts",
            name="ck_background_jobs_background_job_attempts_valid",
        ),
        sa.CheckConstraint(
            "length(idempotency_key) BETWEEN 1 AND 160",
            name="ck_background_jobs_background_job_idempotency_key_valid",
        ),
        sa.CheckConstraint(
            "length(payload_fingerprint) = 64",
            name="ck_background_jobs_background_job_fingerprint_valid",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_background_jobs_tenant_id_tenants",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_background_jobs"),
        sa.UniqueConstraint(
            "tenant_id",
            "idempotency_key",
            name="uq_background_jobs_tenant_idempotency",
        ),
    )
    op.create_index(
        "ix_background_jobs_claim",
        "background_jobs",
        ["status", "run_at", "created_at"],
    )
    op.create_index(
        "ix_background_jobs_tenant_created",
        "background_jobs",
        ["tenant_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_background_jobs_tenant_created", table_name="background_jobs")
    op.drop_index("ix_background_jobs_claim", table_name="background_jobs")
    op.drop_table("background_jobs")
