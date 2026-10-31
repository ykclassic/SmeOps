import uuid
from datetime import datetime, time
from typing import Literal

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Time,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKey


class Service(UUIDPrimaryKey, TimestampMixin, Base):
    __tablename__ = "services"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_services_tenant_id_id"),
        UniqueConstraint("tenant_id", "name", name="uq_services_tenant_name"),
        CheckConstraint("duration_minutes BETWEEN 5 AND 480", name="service_duration_valid"),
        CheckConstraint("price_minor >= 0", name="service_price_nonnegative"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    duration_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    price_minor: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(
        String(3), nullable=False, default="NGN", server_default="NGN"
    )
    is_active: Mapped[bool] = mapped_column(
        nullable=False, default=True, server_default=text("true")
    )


class Customer(UUIDPrimaryKey, TimestampMixin, Base):
    __tablename__ = "customers"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_customers_tenant_id_id"),
        UniqueConstraint("tenant_id", "phone_e164", name="uq_customers_tenant_phone"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    phone_e164: Mapped[str] = mapped_column(String(16), nullable=False)


class BusinessHours(UUIDPrimaryKey, TimestampMixin, Base):
    __tablename__ = "business_hours"
    __table_args__ = (
        UniqueConstraint("tenant_id", "weekday", name="uq_business_hours_tenant_weekday"),
        CheckConstraint("weekday BETWEEN 0 AND 6", name="business_hours_weekday_valid"),
        CheckConstraint(
            "(is_closed = true AND opens_at IS NULL AND closes_at IS NULL) OR "
            "(is_closed = false AND opens_at IS NOT NULL AND closes_at IS NOT NULL "
            "AND opens_at < closes_at)",
            name="business_hours_interval_valid",
        ),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    weekday: Mapped[int] = mapped_column(Integer, nullable=False)
    is_closed: Mapped[bool] = mapped_column(
        nullable=False, default=False, server_default=text("false")
    )
    opens_at: Mapped[time | None] = mapped_column(Time, nullable=True)
    closes_at: Mapped[time | None] = mapped_column(Time, nullable=True)


class Appointment(UUIDPrimaryKey, TimestampMixin, Base):
    __tablename__ = "appointments"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "service_id"],
            ["services.tenant_id", "services.id"],
            name="fk_appointments_tenant_service",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "customer_id"],
            ["customers.tenant_id", "customers.id"],
            name="fk_appointments_tenant_customer",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "created_by_user_id"],
            ["users.tenant_id", "users.id"],
            name="fk_appointments_tenant_creator",
            ondelete="RESTRICT",
        ),
        CheckConstraint("ends_at > starts_at", name="appointment_positive_duration"),
        CheckConstraint(
            "status IN ('confirmed', 'cancelled')",
            name="appointment_status_valid",
        ),
        Index("ix_appointments_tenant_start", "tenant_id", "starts_at"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    service_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    customer_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    created_by_user_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[Literal["confirmed", "cancelled"]] = mapped_column(
        String(16), nullable=False, default="confirmed", server_default=text("'confirmed'")
    )


class AuditEvent(UUIDPrimaryKey, Base):
    __tablename__ = "audit_events"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "actor_user_id"],
            ["users.tenant_id", "users.id"],
            name="fk_audit_events_tenant_actor",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "length(event_type) BETWEEN 1 AND 120",
            name="audit_event_type_valid",
        ),
        Index("ix_audit_events_tenant_created", "tenant_id", "created_at"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    event_type: Mapped[str] = mapped_column(String(120), nullable=False)
    subject_type: Mapped[str] = mapped_column(String(80), nullable=False)
    subject_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
