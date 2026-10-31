import uuid
from typing import Literal

from sqlalchemy import CheckConstraint, ForeignKey, String, UniqueConstraint, Uuid, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKey


class User(UUIDPrimaryKey, TimestampMixin, Base):
    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("tenant_id", "email", name="uq_users_tenant_email"),
        CheckConstraint(
            "role IN ('owner', 'manager', 'staff')",
            name="user_role_valid",
        ),
        CheckConstraint(
            "status IN ('active', 'disabled')",
            name="user_status_valid",
        ),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    email: Mapped[str] = mapped_column(String(254), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[Literal["owner", "manager", "staff"]] = mapped_column(
        String(16), nullable=False, default="owner", server_default=text("'owner'")
    )
    status: Mapped[Literal["active", "disabled"]] = mapped_column(
        String(16), nullable=False, default="active", server_default=text("'active'")
    )
