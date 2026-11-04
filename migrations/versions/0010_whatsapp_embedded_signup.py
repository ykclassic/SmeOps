"""Add Meta Embedded Signup connection lifecycle fields.

Revision ID: 0010_whatsapp_embedded_signup
Revises: 0009_whatsapp_opt_out
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010_whatsapp_embedded_signup"
down_revision: str | None = "0009_whatsapp_opt_out"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(
        "ck_whatsapp_connections_whatsapp_connection_status_valid",
        "whatsapp_connections",
        type_="check",
    )
    op.create_check_constraint(
        "ck_whatsapp_connections_whatsapp_connection_status_valid",
        "whatsapp_connections",
        "status IN ('pending', 'active', 'disabled')",
    )
    op.add_column(
        "whatsapp_connections",
        sa.Column("display_phone_number", sa.String(length=40), nullable=True),
    )
    op.add_column(
        "whatsapp_connections",
        sa.Column("verified_name", sa.String(length=160), nullable=True),
    )
    op.add_column(
        "whatsapp_connections",
        sa.Column("token_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "whatsapp_connections",
        sa.Column("two_step_verification_pin_encrypted", sa.Text(), nullable=True),
    )
    op.add_column(
        "whatsapp_connections",
        sa.Column("onboarding_error_code", sa.String(length=80), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("whatsapp_connections", "onboarding_error_code")
    op.drop_column("whatsapp_connections", "two_step_verification_pin_encrypted")
    op.drop_column("whatsapp_connections", "token_expires_at")
    op.drop_column("whatsapp_connections", "verified_name")
    op.drop_column("whatsapp_connections", "display_phone_number")
    op.drop_constraint(
        "ck_whatsapp_connections_whatsapp_connection_status_valid",
        "whatsapp_connections",
        type_="check",
    )
    op.create_check_constraint(
        "ck_whatsapp_connections_whatsapp_connection_status_valid",
        "whatsapp_connections",
        "status IN ('active', 'disabled')",
    )
