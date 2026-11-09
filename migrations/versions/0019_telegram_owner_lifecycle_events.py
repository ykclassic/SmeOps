"""Add lifecycle event type to Telegram owner notifications.

Revision ID: 0019_telegram_owner_lifecycle_events
Revises: 0018_telegram_customer_identity
"""
import sqlalchemy as sa
from alembic import op

revision = "0019_tg_owner_lifecycle"
down_revision = "0018_telegram_customer_identity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "telegram_owner_notifications",
        sa.Column(
            "event_type", sa.String(length=80),
            server_default="appointment.created", nullable=False,
        ),
    )
    op.drop_constraint(
        "uq_telegram_owner_notifications_appointment_owner",
        "telegram_owner_notifications",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_telegram_owner_notifications_appointment_owner_event",
        "telegram_owner_notifications",
        ["tenant_id", "owner_user_id", "appointment_id", "event_type"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_telegram_owner_notifications_appointment_owner_event",
        "telegram_owner_notifications",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_telegram_owner_notifications_appointment_owner",
        "telegram_owner_notifications",
        ["tenant_id", "owner_user_id", "appointment_id"],
    )
    op.drop_column("telegram_owner_notifications", "event_type")
