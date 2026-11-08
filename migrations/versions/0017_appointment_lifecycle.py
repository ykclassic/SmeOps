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
    op.execute(
        "UPDATE appointments SET status = 'cancelled' "
        "WHERE status IN ('completed', 'missed')"
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
