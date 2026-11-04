import uuid
from datetime import datetime
from typing import Literal

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKey


class WhatsAppConversation(UUIDPrimaryKey, TimestampMixin, Base):
    __tablename__ = "whatsapp_conversations"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_whatsapp_conversations_tenant_id_id"),
        UniqueConstraint(
            "tenant_id",
            "customer_phone_hash",
            name="uq_whatsapp_conversations_tenant_phone",
        ),
        CheckConstraint(
            "status IN ('active', 'human_required', 'closed')",
            name="whatsapp_conversation_status_valid",
        ),
        Index(
            "ix_whatsapp_conversations_tenant_updated",
            "tenant_id",
            "updated_at",
        ),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    customer_phone_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    customer_phone_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[Literal["active", "human_required", "closed"]] = mapped_column(
        String(24), nullable=False, default="active", server_default=text("'active'")
    )
    context: Mapped[dict] = mapped_column(
        JSON,
        nullable=False,
        default=dict,
        server_default=text("'{}'"),
    )
    last_inbound_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class WhatsAppConversationMessage(UUIDPrimaryKey, TimestampMixin, Base):
    __tablename__ = "whatsapp_conversation_messages"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "conversation_id"],
            ["whatsapp_conversations.tenant_id", "whatsapp_conversations.id"],
            name="fk_whatsapp_conversation_messages_tenant_conversation",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "tenant_id",
            "provider_message_id",
            name="uq_whatsapp_conversation_messages_tenant_provider_id",
        ),
        CheckConstraint(
            "direction IN ('inbound', 'outbound')",
            name="whatsapp_conversation_message_direction_valid",
        ),
        CheckConstraint(
            "length(provider_message_id) BETWEEN 1 AND 255",
            name="whatsapp_conversation_message_provider_id_valid",
        ),
        Index(
            "ix_whatsapp_conversation_messages_tenant_conversation_created",
            "tenant_id",
            "conversation_id",
            "created_at",
        ),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    provider_message_id: Mapped[str] = mapped_column(String(255), nullable=False)
    direction: Mapped[Literal["inbound", "outbound"]] = mapped_column(String(12), nullable=False)
    message_type: Mapped[str] = mapped_column(String(40), nullable=False)
    body_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    provider_timestamp: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    delivery_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="received", server_default=text("'received'")
    )
