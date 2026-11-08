import uuid
from datetime import datetime
from typing import Literal

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKey


class TelegramOwnerLink(UUIDPrimaryKey, TimestampMixin, Base):
    __tablename__ = "telegram_owner_links"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "owner_user_id"],
            ["users.tenant_id", "users.id"],
            name="fk_telegram_owner_links_tenant_owner",
            ondelete="CASCADE",
        ),
        UniqueConstraint("tenant_id", "owner_user_id", name="uq_telegram_owner_links_owner"),
        UniqueConstraint("tenant_id", "chat_id", name="uq_telegram_owner_links_chat"),
        UniqueConstraint("invitation_hash", name="uq_telegram_owner_links_invitation_hash"),
        Index("ix_telegram_owner_links_tenant_linked", "tenant_id", "linked_at"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    owner_user_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    invitation_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    chat_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    linked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class TelegramOwnerNotification(UUIDPrimaryKey, TimestampMixin, Base):
    __tablename__ = "telegram_owner_notifications"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "owner_user_id"],
            ["users.tenant_id", "users.id"],
            name="fk_telegram_owner_notifications_tenant_owner",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "tenant_id", "owner_user_id", "appointment_id",
            name="uq_telegram_owner_notifications_appointment_owner",
        ),
        CheckConstraint(
            "status IN ('queued', 'sending', 'sent', 'failed', 'unknown')",
            name="telegram_owner_notification_status_valid",
        ),
        Index("ix_telegram_owner_notifications_tenant_created", "tenant_id", "created_at"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    owner_user_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    appointment_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("appointments.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[Literal["queued", "sending", "sent", "failed", "unknown"]] = mapped_column(
        String(16), nullable=False, default="queued", server_default=text("'queued'")
    )
    provider_message_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    last_error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
