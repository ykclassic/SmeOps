"""Add voluntary deactivated tenant state.

Revision ID: 0023_tenant_deactivated_status
Revises: 0022_tg_reply_markup

Existing tenant rows are preserved. Downgrade is refused while deactivated rows
exist so that rollback never silently converts or discards lifecycle state.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0023_tenant_deactivated_status"
down_revision: str | None = "0022_tg_reply_markup"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CONSTRAINT_NAME = "ck_tenants_tenant_status_valid"


def upgrade() -> None:
    op.drop_constraint(CONSTRAINT_NAME, "tenants", type_="check")
    op.create_check_constraint(
        CONSTRAINT_NAME,
        "tenants",
        "status IN ('active', 'deactivated', 'suspended', 'pending')",
    )


def downgrade() -> None:
    bind = op.get_bind()
    has_deactivated_tenants = bind.execute(
        sa.text("SELECT EXISTS (SELECT 1 FROM tenants WHERE status = 'deactivated')")
    ).scalar_one()

    if has_deactivated_tenants:
        raise RuntimeError(
            "Cannot downgrade tenant status constraint while deactivated tenants exist. "
            "Reactivate or explicitly migrate those tenants before retrying."
        )

    op.drop_constraint(CONSTRAINT_NAME, "tenants", type_="check")
    op.create_check_constraint(
        CONSTRAINT_NAME,
        "tenants",
        "status IN ('active', 'suspended', 'pending')",
    )
