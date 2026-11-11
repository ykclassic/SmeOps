from typing import Literal

from sqlalchemy import CheckConstraint, String, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKey


class Tenant(UUIDPrimaryKey, TimestampMixin, Base):
    __tablename__ = "tenants"
    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'deactivated', 'suspended', 'pending')",
            name="tenant_status_valid",
        ),
    )

    name: Mapped[str] = mapped_column(String(160), nullable=False)
    slug: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)
    business_timezone: Mapped[str] = mapped_column(
        String(64), nullable=False, default="Africa/Lagos", server_default="Africa/Lagos"
    )
    status: Mapped[Literal["active", "deactivated", "suspended", "pending"]] = mapped_column(
        String(16), nullable=False, default="pending", server_default=text("'pending'")
    )
