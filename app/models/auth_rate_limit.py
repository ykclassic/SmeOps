from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Index, String, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKey


class AuthRateLimitBucket(UUIDPrimaryKey, TimestampMixin, Base):
    __tablename__ = "auth_rate_limit_buckets"
    __table_args__ = (
        UniqueConstraint("key_hash", name="uq_auth_rate_limit_buckets_key_hash"),
        CheckConstraint("attempts >= 0", name="auth_rate_limit_attempts_nonnegative"),
        Index("ix_auth_rate_limit_buckets_updated_at", "updated_at"),
    )

    key_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    window_started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    attempts: Mapped[int] = mapped_column(
        nullable=False,
        default=0,
        server_default=text("0"),
    )
    blocked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
