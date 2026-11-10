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
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKey


class TelegramConnection(UUIDPrimaryKey, TimestampMixin, Base):
    __tablename__ = "telegram_connections"
    __table_args__ = (
        UniqueConstraint("tenant_id", name="uq_telegram_connections_tenant"),
        UniqueConstraint("bot_id", name="uq_telegram_connections_bot_id"),
        CheckConstraint(
            "status IN ('pending', 'active', 'disabled')",
            name="telegram_connection_status_valid",
        ),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    bot_id: Mapped[str] = mapped_column(String(80), nullable=False)
    bot_username: Mapped[str | None] = mapped_column(String(80), nullable=True)
    bot_token_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    webhook_secret_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[Literal["pending", "active", "disabled"]] = mapped_column(
        String(16), nullable=False, default="pending", server_default=text("'pending'")
    )


class TelegramWebhookEvent(UUIDPrimaryKey, Base):
    __tablename__ = "telegram_webhook_events"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "provider_update_id",
            name="uq_telegram_webhook_events_tenant_update",
        ),
        UniqueConstraint(
            "bot_id", "provider_update_id",
            name="uq_telegram_webhook_events_bot_update",
        ),
        CheckConstraint(
            "status IN ('queued', 'processed', 'failed')",
            name="telegram_webhook_event_status_valid",
        ),
        CheckConstraint(
            "length(payload_sha256) = 64",
            name="telegram_webhook_event_hash_valid",
        ),
        Index("ix_telegram_webhook_events_tenant_received", "tenant_id", "received_at"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    bot_id: Mapped[str] = mapped_column(String(80), nullable=False, default="9016622451")
    provider_update_id: Mapped[str] = mapped_column(String(80), nullable=False)
    event_type: Mapped[str] = mapped_column(String(80), nullable=False)
    payload_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    payload_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[Literal["queued", "processed", "failed"]] = mapped_column(
        String(16), nullable=False, default="queued", server_default=text("'queued'")
    )
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class TelegramConversation(UUIDPrimaryKey, TimestampMixin, Base):
    __tablename__ = "telegram_conversations"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_telegram_conversations_tenant_id_id"),
        UniqueConstraint(
            "tenant_id", "telegram_chat_id",
            name="uq_telegram_conversations_tenant_chat",
        ),
        CheckConstraint(
            "status IN ('active', 'human_required', 'closed')",
            name="telegram_conversation_status_valid",
        ),
        Index("ix_telegram_conversations_tenant_updated", "tenant_id", "updated_at"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    telegram_user_id: Mapped[str] = mapped_column(String(80), nullable=False)
    telegram_chat_id: Mapped[str] = mapped_column(String(80), nullable=False)
    customer_name: Mapped[str | None] = mapped_column(String(160), nullable=True)
    customer_phone_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[Literal["active", "human_required", "closed"]] = mapped_column(
        String(24), nullable=False, default="active", server_default=text("'active'")
    )
    context: Mapped[dict] = mapped_column(
        JSON, nullable=False, default=dict, server_default=text("'{}'")
    )
    last_inbound_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class TelegramConversationMessage(UUIDPrimaryKey, TimestampMixin, Base):
    __tablename__ = "telegram_conversation_messages"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "conversation_id"],
            ["telegram_conversations.tenant_id", "telegram_conversations.id"],
            name="fk_telegram_messages_tenant_conversation",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "tenant_id", "provider_message_id",
            name="uq_telegram_messages_tenant_provider_id",
        ),
        CheckConstraint(
            "direction IN ('inbound', 'outbound')",
            name="telegram_message_direction_valid",
        ),
        Index(
            "ix_telegram_messages_tenant_conversation_created",
            "tenant_id", "conversation_id", "created_at",
        ),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    provider_message_id: Mapped[str] = mapped_column(String(80), nullable=False)
    direction: Mapped[Literal["inbound", "outbound"]] = mapped_column(String(12), nullable=False)
    body_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    contact_phone_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    contact_user_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    delivery_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="received", server_default=text("'received'")
    )


class TelegramOutboundMessage(UUIDPrimaryKey, TimestampMixin, Base):
    __tablename__ = "telegram_outbound_messages"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "conversation_id"],
            ["telegram_conversations.tenant_id", "telegram_conversations.id"],
            name="fk_telegram_outbound_messages_tenant_id_telegram_conversations",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "tenant_id", "idempotency_key",
            name="uq_telegram_outbound_tenant_idempotency",
        ),
        CheckConstraint(
            "status IN ('queued', 'sending', 'sent', 'failed', 'unknown')",
            name="telegram_outbound_status_valid",
        ),
        Index("ix_telegram_outbound_tenant_created", "tenant_id", "created_at"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    recipient_chat_id: Mapped[str] = mapped_column(String(80), nullable=False)
    body_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    reply_markup: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(160), nullable=False)
    status: Mapped[Literal["queued", "sending", "sent", "failed", "unknown"]] = mapped_column(
        String(16), nullable=False, default="queued", server_default=text("'queued'")
    )
    provider_message_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    last_error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)



class TelegramPlatformBot(UUIDPrimaryKey, TimestampMixin, Base):
    """Singleton platform-managed Telegram bot credential; never tenant supplied."""

    __tablename__ = "telegram_platform_bots"
    __table_args__ = (
        UniqueConstraint("bot_id", name="uq_telegram_platform_bots_bot_id"),
        UniqueConstraint("singleton_key", name="uq_telegram_platform_bots_singleton_key"),
        CheckConstraint("singleton_key = 'platform'", name="telegram_platform_bot_singleton"),
        CheckConstraint("status IN ('active', 'disabled')", name="telegram_platform_bot_status"),
    )

    singleton_key: Mapped[str] = mapped_column(
        String(16), nullable=False, default="platform", server_default=text("'platform'")
    )
    bot_id: Mapped[str] = mapped_column(String(80), nullable=False)
    bot_username: Mapped[str] = mapped_column(String(80), nullable=False)
    bot_token_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    webhook_secret_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[Literal["active", "disabled"]] = mapped_column(
        String(16), nullable=False, default="active", server_default=text("'active'")
    )


class TelegramBookingReference(UUIDPrimaryKey, TimestampMixin, Base):
    """Opaque customer-facing business link; stores only a SHA-256 token hash."""

    __tablename__ = "telegram_booking_references"
    __table_args__ = (
        UniqueConstraint("token_hash", name="uq_telegram_booking_references_token_hash"),
        UniqueConstraint("tenant_id", name="uq_telegram_booking_references_tenant"),
        Index("ix_telegram_booking_references_tenant_active", "tenant_id", "revoked_at"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    token_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class TelegramChatContext(UUIDPrimaryKey, TimestampMixin, Base):
    """Current business selection for a private chat on the shared platform bot."""

    __tablename__ = "telegram_chat_contexts"
    __table_args__ = (
        UniqueConstraint(
            "platform_bot_id", "telegram_chat_id",
            name="uq_telegram_chat_contexts_bot_chat",
        ),
        Index("ix_telegram_chat_contexts_tenant", "tenant_id"),
    )

    platform_bot_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("telegram_platform_bots.id", ondelete="CASCADE"),
        nullable=False,
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    telegram_user_id: Mapped[str] = mapped_column(String(80), nullable=False)
    telegram_chat_id: Mapped[str] = mapped_column(String(80), nullable=False)
    selected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )



class TelegramCustomerIdentity(UUIDPrimaryKey, TimestampMixin, Base):
    """A customer identity verified through Telegram's self-shared contact action."""

    __tablename__ = "telegram_customer_identities"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "customer_id"],
            ["customers.tenant_id", "customers.id"],
            name="fk_telegram_customer_identities_tenant_customer",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "tenant_id", "telegram_user_id",
            name="uq_telegram_customer_identity_tenant_user",
        ),
        UniqueConstraint(
            "tenant_id", "customer_id",
            name="uq_telegram_customer_identity_tenant_customer",
        ),
        UniqueConstraint(
            "tenant_id", "telegram_chat_id",
            name="uq_telegram_customer_identity_tenant_chat",
        ),
        Index(
            "ix_telegram_customer_identity_active",
            "tenant_id", "telegram_user_id", "revoked_at",
        ),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(
            "tenants.id",
            name="fk_tg_customer_identity_tenant",
            ondelete="CASCADE",
        ),
        nullable=False,
    )
    customer_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    platform_bot_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(
            "telegram_platform_bots.id",
            name="fk_tg_customer_identity_platform_bot",
            ondelete="CASCADE",
        ),
        nullable=False,
    )
    telegram_user_id: Mapped[str] = mapped_column(String(80), nullable=False)
    telegram_chat_id: Mapped[str] = mapped_column(String(80), nullable=False)
    verified_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
