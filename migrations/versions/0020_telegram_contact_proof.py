"""Persist Telegram shared-contact proof separately from user text.

Revision ID: 0020_telegram_contact_proof
Revises: 0019_tg_owner_lifecycle
"""
import sqlalchemy as sa
from alembic import op

revision = "0020_telegram_contact_proof"
down_revision = "0019_tg_owner_lifecycle"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "telegram_conversation_messages",
        sa.Column("contact_phone_encrypted", sa.Text(), nullable=True),
    )
    op.add_column(
        "telegram_conversation_messages",
        sa.Column("contact_user_id", sa.String(length=80), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("telegram_conversation_messages", "contact_user_id")
    op.drop_column("telegram_conversation_messages", "contact_phone_encrypted")
