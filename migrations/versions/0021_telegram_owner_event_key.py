"""Give each Telegram owner alert a unique lifecycle event key.

Revision ID: 0021_tg_owner_event_key
Revises: 0020_telegram_contact_proof
"""
import sqlalchemy as sa
from alembic import op

revision = "0021_tg_owner_event_key"
down_revision = "0020_telegram_contact_proof"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "telegram_owner_notifications",
        sa.Column("event_key", sa.String(length=160), nullable=True),
    )
    op.execute(
        """
        UPDATE telegram_owner_notifications
        SET event_key = event_type || ':' || appointment_id::text
        WHERE event_key IS NULL
        """
    )
    op.alter_column(
        "telegram_owner_notifications",
        "event_key",
        existing_type=sa.String(length=160),
        nullable=False,
    )
    op.drop_constraint(
        "uq_telegram_owner_notifications_appointment_owner_event",
        "telegram_owner_notifications",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_telegram_owner_notifications_event_key",
        "telegram_owner_notifications",
        ["tenant_id", "owner_user_id", "event_key"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_telegram_owner_notifications_event_key",
        "telegram_owner_notifications",
        type_="unique",
    )
    op.drop_column("telegram_owner_notifications", "event_key")
    op.create_unique_constraint(
        "uq_telegram_owner_notifications_appointment_owner_event",
        "telegram_owner_notifications",
        ["tenant_id", "owner_user_id", "appointment_id", "event_type"],
    )
