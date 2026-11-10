"""Persist Telegram reply keyboards with outbound messages.

Revision ID: 0022_tg_reply_markup
Revises: 0021_tg_owner_event_key
"""
import sqlalchemy as sa
from alembic import op

revision = "0022_tg_reply_markup"
down_revision = "0021_tg_owner_event_key"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("telegram_outbound_messages", sa.Column("reply_markup", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("telegram_outbound_messages", "reply_markup")
