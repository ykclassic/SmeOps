"""Add WhatsApp customer messaging opt-out state.

Revision ID: 0009_whatsapp_opt_out
Revises: 0008_whatsapp_conversations
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009_whatsapp_opt_out"
down_revision: str | None = "0008_whatsapp_conversations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "whatsapp_conversations",
        sa.Column(
            "is_opted_out",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
    )
    op.add_column(
        "whatsapp_conversations",
        sa.Column("opted_out_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("whatsapp_conversations", "opted_out_at")
    op.drop_column("whatsapp_conversations", "is_opted_out")
