import uuid
from datetime import datetime
from typing import Literal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKey

OutboundStatus = Literal[
    "queued", "sending", "sent", "delivered", "read", "failed", "unknown"
]


class WhatsAppOutboundMessage(UUIDPrimaryKey, TimestampMixin, Base):
    __tablename__ = "whatsapp_outbound_messages"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "idempotency_key",
            name="uq_whatsapp_outbound_messages_tenant_idempotency",
        ),
        UniqueConstraint(
            "provider_message_id",
            name="uq_whatsapp_outbound_messages_provider_id",
        ),
        CheckConstraint(
            "status IN ('queued', 'sending', 'sent', 'delivered', 'read', 'failed', 'unknown')",
            name="whatsapp_outbound_message_status_valid",
        ),
        CheckConstraint(
            "length(idempotency_key) BETWEEN 1 AND 160",
            name="whatsapp_outbound_message_idempotency_valid",
        ),
        Index(
            "ix_whatsapp_outbound_messages_tenant_created",
            "tenant_id",
            "created_at",
        ),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    recipient_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    body_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(160), nullable=False)
    status: Mapped[OutboundStatus] = mapped_column(
        String(16), nullable=False, default="queued", server_default=text("'queued'")
    )
    deliver_after_deactivation: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    provider_message_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    last_error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
