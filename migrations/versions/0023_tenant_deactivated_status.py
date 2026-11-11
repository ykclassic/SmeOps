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


def _drop_tenant_status_check() -> None:
    """Drop the existing status check regardless of its historical DB name.

    The initial migration supplied an already-prefixed explicit name while the
    metadata naming convention also prefixes explicit names. Inspect the
    catalog instead of assuming which physical name was created.
    """
    bind = op.get_bind()
    names = bind.execute(
        sa.text(
            """
            SELECT conname
            FROM pg_constraint
            WHERE conrelid = 'tenants'::regclass
              AND contype = 'c'
              AND pg_get_constraintdef(oid) ILIKE '%status%'
            """
        )
    ).scalars().all()

    if len(names) != 1:
        raise RuntimeError(
            "Expected exactly one tenant status CHECK constraint; "
            f"found {len(names)}: {names!r}"
        )

    op.drop_constraint(sa.schema.conv(names[0]), "tenants", type_="check")


def upgrade() -> None:
    _drop_tenant_status_check()
    op.create_check_constraint(
        "tenant_status_valid",
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

    _drop_tenant_status_check()
    op.create_check_constraint(
        "tenant_status_valid",
        "tenants",
        "status IN ('active', 'suspended', 'pending')",
    )
