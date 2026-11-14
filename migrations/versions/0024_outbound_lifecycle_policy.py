"""Persist delivery eligibility for committed appointment notifications."""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0024_outbound_lifecycle_policy"
down_revision: str | None = "0023_tenant_deactivated_status"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    for table_name in ("whatsapp_outbound_messages", "telegram_outbound_messages"):
        op.add_column(
            table_name,
            sa.Column(
                "deliver_after_deactivation",
                sa.Boolean(),
                server_default=sa.false(),
                nullable=False,
            ),
        )


def downgrade() -> None:
    bind = op.get_bind()
    has_preserved_messages = bind.execute(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM whatsapp_outbound_messages "
            "WHERE deliver_after_deactivation IS TRUE) OR EXISTS "
            "(SELECT 1 FROM telegram_outbound_messages "
            "WHERE deliver_after_deactivation IS TRUE)"
        )
    ).scalar_one()
    if has_preserved_messages:
        raise RuntimeError("Cannot downgrade while preserved appointment notifications remain.")
    op.drop_column("telegram_outbound_messages", "deliver_after_deactivation")
    op.drop_column("whatsapp_outbound_messages", "deliver_after_deactivation")
