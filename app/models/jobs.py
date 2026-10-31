import uuid
from datetime import UTC, datetime
from typing import Literal

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKey


class BackgroundJob(UUIDPrimaryKey, TimestampMixin, Base):
    __tablename__ = "background_jobs"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "idempotency_key",
            name="uq_background_jobs_tenant_idempotency",
        ),
        CheckConstraint(
            "status IN ('queued', 'running', 'completed', 'dead_letter')",
            name="background_job_status_valid",
        ),
        CheckConstraint(
            "attempts >= 0 AND max_attempts BETWEEN 1 AND 10 AND attempts <= max_attempts",
            name="background_job_attempts_valid",
        ),
        CheckConstraint(
            "length(idempotency_key) BETWEEN 1 AND 160",
            name="background_job_idempotency_key_valid",
        ),
        CheckConstraint(
            "length(payload_fingerprint) = 64",
            name="background_job_fingerprint_valid",
        ),
        Index("ix_background_jobs_claim", "status", "run_at", "created_at"),
        Index("ix_background_jobs_tenant_created", "tenant_id", "created_at"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    job_type: Mapped[str] = mapped_column(String(80), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(160), nullable=False)
    payload_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[Literal["queued", "running", "completed", "dead_letter"]] = mapped_column(
        String(16), nullable=False, default="queued", server_default=text("'queued'")
    )
    attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    max_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=3, server_default="3"
    )
    run_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    locked_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    last_error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
