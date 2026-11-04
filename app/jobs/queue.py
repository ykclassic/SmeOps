import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import and_, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import BackgroundJob


class JobIdempotencyConflict(ValueError):
    """An idempotency key was reused with a different job payload."""


class JobLeaseLost(RuntimeError):
    """The worker no longer owns the claimed job lease."""


@dataclass(frozen=True)
class ClaimedJob:
    id: UUID
    tenant_id: UUID
    job_type: str
    payload: dict
    attempts: int
    max_attempts: int


def _payload_fingerprint(job_type: str, payload: dict) -> str:
    canonical = json.dumps(
        {"job_type": job_type, "payload": payload},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


async def enqueue_job(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    job_type: str,
    payload: dict,
    idempotency_key: str,
    max_attempts: int = 3,
    run_at: datetime | None = None,
) -> BackgroundJob:
    if not re.fullmatch(r"[a-z][a-z0-9_.-]{1,79}", job_type):
        raise ValueError("Invalid job type")
    if not 1 <= max_attempts <= 10:
        raise ValueError("max_attempts must be between 1 and 10")
    if not 1 <= len(idempotency_key) <= 160:
        raise ValueError("idempotency_key must contain 1 to 160 characters")
    if run_at is not None and run_at.tzinfo is None:
        raise ValueError("run_at must be timezone-aware")

    fingerprint = _payload_fingerprint(job_type, payload)
    existing_result = await session.execute(
        select(BackgroundJob).where(
            BackgroundJob.tenant_id == tenant_id,
            BackgroundJob.idempotency_key == idempotency_key,
        )
    )
    existing = existing_result.scalar_one_or_none()
    if existing is not None:
        if existing.payload_fingerprint != fingerprint:
            raise JobIdempotencyConflict("Idempotency key was used with a different payload")
        return existing

    job = BackgroundJob(
        id=uuid4(),
        tenant_id=tenant_id,
        job_type=job_type,
        payload=payload,
        idempotency_key=idempotency_key,
        payload_fingerprint=fingerprint,
        max_attempts=max_attempts,
        run_at=run_at.astimezone(UTC) if run_at is not None else datetime.now(UTC),
    )
    try:
        async with session.begin_nested():
            session.add(job)
            await session.flush()
    except IntegrityError:
        duplicate_result = await session.execute(
            select(BackgroundJob).where(
                BackgroundJob.tenant_id == tenant_id,
                BackgroundJob.idempotency_key == idempotency_key,
            )
        )
        duplicate = duplicate_result.scalar_one_or_none()
        if duplicate is None or duplicate.payload_fingerprint != fingerprint:
            raise JobIdempotencyConflict(
                "Idempotency key was concurrently used with a different payload"
            ) from None
        return duplicate
    return job


async def claim_next_job(
    session: AsyncSession,
    *,
    worker_id: str,
    lease_seconds: int = 60,
) -> ClaimedJob | None:
    if not 1 <= len(worker_id) <= 120:
        raise ValueError("worker_id must contain 1 to 120 characters")
    if lease_seconds < 10:
        raise ValueError("lease_seconds must be at least 10")

    now = datetime.now(UTC)
    stale_before = now - timedelta(seconds=lease_seconds)

    await session.execute(
        update(BackgroundJob)
        .where(
            BackgroundJob.status == "running",
            BackgroundJob.locked_at < stale_before,
            BackgroundJob.attempts >= BackgroundJob.max_attempts,
        )
        .values(
            status="dead_letter",
            locked_at=None,
            locked_by=None,
            last_error_code="worker_lease_expired",
        )
    )

    eligible = or_(
        and_(BackgroundJob.status == "queued", BackgroundJob.run_at <= now),
        and_(
            BackgroundJob.status == "running",
            BackgroundJob.locked_at < stale_before,
            BackgroundJob.attempts < BackgroundJob.max_attempts,
        ),
    )
    result = await session.execute(
        select(BackgroundJob)
        .where(eligible)
        .order_by(BackgroundJob.run_at, BackgroundJob.created_at)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    job = result.scalar_one_or_none()
    if job is None:
        await session.commit()
        return None

    job.status = "running"
    job.attempts += 1
    job.locked_at = now
    job.locked_by = worker_id
    await session.commit()

    return ClaimedJob(
        id=job.id,
        tenant_id=job.tenant_id,
        job_type=job.job_type,
        payload=job.payload,
        attempts=job.attempts,
        max_attempts=job.max_attempts,
    )


async def complete_job(
    session: AsyncSession,
    *,
    job_id: UUID,
    worker_id: str,
) -> None:
    result = await session.execute(
        select(BackgroundJob)
        .where(
            BackgroundJob.id == job_id,
            BackgroundJob.status == "running",
            BackgroundJob.locked_by == worker_id,
        )
        .with_for_update()
    )
    job = result.scalar_one_or_none()
    if job is None:
        raise JobLeaseLost("Worker does not own this job lease")

    job.status = "completed"
    job.locked_at = None
    job.locked_by = None
    job.last_error_code = None
    await session.commit()


async def fail_job(
    session: AsyncSession,
    *,
    job_id: UUID,
    worker_id: str,
    error_code: str = "handler_failed",
) -> None:
    result = await session.execute(
        select(BackgroundJob)
        .where(
            BackgroundJob.id == job_id,
            BackgroundJob.status == "running",
            BackgroundJob.locked_by == worker_id,
        )
        .with_for_update()
    )
    job = result.scalar_one_or_none()
    if job is None:
        raise JobLeaseLost("Worker does not own this job lease")

    safe_error_code = (
        error_code if re.fullmatch(r"[a-z][a-z0-9_]{0,79}", error_code) else "handler_failed"
    )
    job.last_error_code = safe_error_code
    job.locked_at = None
    job.locked_by = None
    if job.attempts >= job.max_attempts:
        job.status = "dead_letter"
    else:
        delay_seconds = min(5 * (2 ** (job.attempts - 1)), 300)
        job.status = "queued"
        job.run_at = datetime.now(UTC) + timedelta(seconds=delay_seconds)
    await session.commit()
