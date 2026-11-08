"""Add appointment lifecycle states and timestamps.

Revision ID: 0017_appointment_lifecycle
Revises: 0016_tg_platform_secret
"""
import sqlalchemy as sa
from alembic import op

revision = "0017_appointment_lifecycle"
down_revision = "0016_tg_platform_secret"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint(
        "ck_appointments_appointment_status_valid",
        "appointments",
        type_="check",
    )
    op.create_check_constraint(
        "ck_appointments_appointment_status_valid",
        "appointments",
        "status IN ('confirmed', 'cancelled', 'completed', 'missed')",
    )
    op.add_column("appointments", sa.Column("cancelled_at", sa.DateTime(timezone=True)))
    op.add_column("appointments", sa.Column("completed_at", sa.DateTime(timezone=True)))
    op.add_column("appointments", sa.Column("missed_at", sa.DateTime(timezone=True)))
    op.add_column("appointments", sa.Column("rescheduled_at", sa.DateTime(timezone=True)))


def downgrade() -> None:
    bind = op.get_bind()
    lifecycle_rows = bind.execute(
        sa.text(
            "SELECT count(*) FROM appointments "
            "WHERE status IN ('completed', 'missed')"
        )
    ).scalar_one()
    if lifecycle_rows:
        raise RuntimeError(
            "Cannot downgrade while completed or missed appointments exist; "
            "export and reconcile lifecycle records before downgrading."
        )
    op.drop_column("appointments", "rescheduled_at")
    op.drop_column("appointments", "missed_at")
    op.drop_column("appointments", "completed_at")
    op.drop_column("appointments", "cancelled_at")
    op.drop_constraint(
        "ck_appointments_appointment_status_valid",
        "appointments",
        type_="check",
    )
    op.create_check_constraint(
        "ck_appointments_appointment_status_valid",
        "appointments",
        "status IN ('confirmed', 'cancelled')",
    )
