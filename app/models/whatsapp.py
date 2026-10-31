import uuid
from datetime import datetime
from typing import Literal

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKey


class WhatsAppConnection(UUIDPrimaryKey, TimestampMixin, Base):
    __tablename__ = "whatsapp_connections"
    __table_args__ = (
        UniqueConstraint("tenant_id", name="uq_whatsapp_connections_tenant"),
        UniqueConstraint("phone_number_id", name="uq_whatsapp_connections_phone_number"),
        CheckConstraint(
            "status IN ('active', 'disabled')",
            name="whatsapp_connection_status_valid",
        ),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    phone_number_id: Mapped[str] = mapped_column(String(80), nullable=False)
    business_account_id: Mapped[str] = mapped_column(String(80), nullable=False)
    access_token_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[Literal["active", "disabled"]] = mapped_column(
        String(16), nullable=False, default="active", server_default=text("'active'")
    )


class WhatsAppWebhookEvent(UUIDPrimaryKey, Base):
    __tablename__ = "whatsapp_webhook_events"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "provider_event_id",
            name="uq_whatsapp_webhook_events_tenant_provider_event",
        ),
        CheckConstraint(
            "status IN ('queued', 'accepted', 'processed', 'failed')",
            name="whatsapp_webhook_event_status_valid",
        ),
        CheckConstraint(
            "length(payload_sha256) = 64",
            name="whatsapp_webhook_event_hash_valid",
        ),
        Index(
            "ix_whatsapp_webhook_events_tenant_received",
            "tenant_id",
            "received_at",
        ),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    provider_event_id: Mapped[str] = mapped_column(String(255), nullable=False)
    event_type: Mapped[str] = mapped_column(String(120), nullable=False)
    payload_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    payload_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[Literal["queued", "accepted", "processed", "failed"]] = mapped_column(
        String(16), nullable=False, default="queued", server_default=text("'queued'")
    )
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
