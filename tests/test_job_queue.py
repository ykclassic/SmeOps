from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select, update

from app.db.session import AsyncSessionFactory
from app.jobs.queue import (
    JobIdempotencyConflict,
    claim_next_job,
    complete_job,
    enqueue_job,
    fail_job,
)
from app.models import BackgroundJob, Tenant


async def _new_tenant() -> Tenant:
    tenant = Tenant(
        id=uuid4(),
        name="Queue Test Business",
        slug=f"queue-{uuid4().hex[:12]}",
        business_timezone="Africa/Lagos",
        status="active",
    )
    async with AsyncSessionFactory() as session:
        session.add(tenant)
        await session.commit()
    return tenant


@pytest.mark.asyncio
async def test_job_enqueue_is_idempotent_and_worker_can_complete() -> None:
    tenant = await _new_tenant()
    key = f"unit-test:{uuid4()}"

    async with AsyncSessionFactory() as session:
        first = await enqueue_job(
            session,
            tenant_id=tenant.id,
            job_type="test.noop",
            payload={"record_id": "sample-123"},
            idempotency_key=key,
        )
        await session.commit()
        first_id = first.id

    async with AsyncSessionFactory() as session:
        replay = await enqueue_job(
            session,
            tenant_id=tenant.id,
            job_type="test.noop",
            payload={"record_id": "sample-123"},
            idempotency_key=key,
        )
        assert replay.id == first_id
        with pytest.raises(JobIdempotencyConflict):
            await enqueue_job(
                session,
                tenant_id=tenant.id,
                job_type="test.noop",
                payload={"record_id": "different"},
                idempotency_key=key,
            )

    async with AsyncSessionFactory() as session:
        claimed = await claim_next_job(session, worker_id="test-worker")
    assert claimed is not None
    assert claimed.id == first_id
    assert claimed.attempts == 1

    async with AsyncSessionFactory() as session:
        await complete_job(session, job_id=first_id, worker_id="test-worker")

    async with AsyncSessionFactory() as session:
        result = await session.execute(
            select(BackgroundJob).where(BackgroundJob.id == first_id)
        )
        completed = result.scalar_one()
        assert completed.status == "completed"
        assert completed.locked_at is None
        assert completed.locked_by is None


@pytest.mark.asyncio
async def test_job_retries_then_moves_to_dead_letter() -> None:
    tenant = await _new_tenant()
    async with AsyncSessionFactory() as session:
        job = await enqueue_job(
            session,
            tenant_id=tenant.id,
            job_type="test.failing",
            payload={"safe": True},
            idempotency_key=f"unit-test:{uuid4()}",
            max_attempts=2,
        )
        await session.commit()
        job_id = job.id

    async with AsyncSessionFactory() as session:
        first_claim = await claim_next_job(session, worker_id="retry-worker")
    assert first_claim is not None
    assert first_claim.attempts == 1

    async with AsyncSessionFactory() as session:
        await fail_job(
            session,
            job_id=job_id,
            worker_id="retry-worker",
            error_code="temporary_failure",
        )

    async with AsyncSessionFactory() as session:
        await session.execute(
            update(BackgroundJob)
            .where(BackgroundJob.id == job_id)
            .values(run_at=datetime.now(UTC) - timedelta(seconds=1))
        )
        await session.commit()

    async with AsyncSessionFactory() as session:
        second_claim = await claim_next_job(session, worker_id="retry-worker")
    assert second_claim is not None
    assert second_claim.attempts == 2

    async with AsyncSessionFactory() as session:
        await fail_job(
            session,
            job_id=job_id,
            worker_id="retry-worker",
            error_code="temporary_failure",
        )

    async with AsyncSessionFactory() as session:
        result = await session.execute(
            select(BackgroundJob).where(BackgroundJob.id == job_id)
        )
        dead_letter = result.scalar_one()
        assert dead_letter.status == "dead_letter"
        assert dead_letter.last_error_code == "temporary_failure"
        assert dead_letter.attempts == 2


@pytest.mark.asyncio
async def test_telegram_lifecycle_job_retries_then_dead_letters_after_max_attempts() -> None:
    tenant = await _new_tenant()
    worker_id = f"telegram-lifecycle-worker-{uuid4().hex}"
    async with AsyncSessionFactory() as session:
        job = await enqueue_job(
            session,
            tenant_id=tenant.id,
            job_type="telegram.accept_inbound",
            payload={"webhook_event_id": str(uuid4())},
            idempotency_key=f"telegram-lifecycle:{uuid4()}",
            max_attempts=2,
        )
        job_id = job.id
        job.status = "running"
        job.attempts = 1
        job.locked_at = datetime.now(UTC)
        job.locked_by = worker_id
        await session.commit()

    async with AsyncSessionFactory() as session:
        await fail_job(
            session,
            job_id=job_id,
            worker_id=worker_id,
            error_code="temporary_database_failure",
        )

    async with AsyncSessionFactory() as session:
        retry = await session.get(BackgroundJob, job_id)
        assert retry is not None
        assert retry.status == "queued"
        assert retry.attempts == 1
        assert retry.run_at > datetime.now(UTC)
        assert retry.locked_at is None
        assert retry.locked_by is None

        retry.status = "running"
        retry.attempts = 2
        retry.locked_at = datetime.now(UTC)
        retry.locked_by = worker_id
        await session.commit()

    async with AsyncSessionFactory() as session:
        await fail_job(
            session,
            job_id=job_id,
            worker_id=worker_id,
            error_code="temporary_database_failure",
        )

    async with AsyncSessionFactory() as session:
        dead_letter = await session.get(BackgroundJob, job_id)
        assert dead_letter is not None
        assert dead_letter.status == "dead_letter"
        assert dead_letter.attempts == 2
        assert dead_letter.last_error_code == "temporary_database_failure"
        assert dead_letter.locked_at is None
        assert dead_letter.locked_by is None


@pytest.mark.asyncio
async def test_whatsapp_lifecycle_job_retries_then_dead_letters_after_max_attempts() -> None:
    tenant = await _new_tenant()
    worker_id = f"whatsapp-lifecycle-worker-{uuid4().hex}"
    async with AsyncSessionFactory() as session:
        job = await enqueue_job(
            session,
            tenant_id=tenant.id,
            job_type="whatsapp.accept_inbound",
            payload={"webhook_event_id": str(uuid4())},
            idempotency_key=f"whatsapp-lifecycle:{uuid4()}",
            max_attempts=2,
        )
        job_id = job.id
        job.status = "running"
        job.attempts = 1
        job.locked_at = datetime.now(UTC)
        job.locked_by = worker_id
        await session.commit()

    async with AsyncSessionFactory() as session:
        await fail_job(
            session,
            job_id=job_id,
            worker_id=worker_id,
            error_code="temporary_database_failure",
        )

    async with AsyncSessionFactory() as session:
        retry = await session.get(BackgroundJob, job_id)
        assert retry is not None
        assert retry.status == "queued"
        assert retry.attempts == 1
        assert retry.run_at > datetime.now(UTC)
        assert retry.locked_at is None
        assert retry.locked_by is None

        retry.status = "running"
        retry.attempts = 2
        retry.locked_at = datetime.now(UTC)
        retry.locked_by = worker_id
        await session.commit()

    async with AsyncSessionFactory() as session:
        await fail_job(
            session,
            job_id=job_id,
            worker_id=worker_id,
            error_code="temporary_database_failure",
        )

    async with AsyncSessionFactory() as session:
        dead_letter = await session.get(BackgroundJob, job_id)
        assert dead_letter is not None
        assert dead_letter.status == "dead_letter"
        assert dead_letter.attempts == 2
        assert dead_letter.last_error_code == "temporary_database_failure"
        assert dead_letter.locked_at is None
        assert dead_letter.locked_by is None


@pytest.mark.asyncio
async def test_expired_worker_lease_is_reclaimed_without_losing_attempt_count() -> None:
    tenant = await _new_tenant()
    original_worker = f"original-worker-{uuid4().hex}"
    recovery_worker = f"recovery-worker-{uuid4().hex}"
    async with AsyncSessionFactory() as session:
        job = await enqueue_job(
            session,
            tenant_id=tenant.id,
            job_type="whatsapp.accept_inbound",
            payload={"webhook_event_id": str(uuid4())},
            idempotency_key=f"lease-recovery:{uuid4()}",
            max_attempts=3,
        )
        job_id = job.id
        job.status = "running"
        job.attempts = 1
        job.locked_at = datetime.now(UTC) - timedelta(minutes=5)
        job.locked_by = original_worker
        await session.commit()

    async with AsyncSessionFactory() as session:
        reclaimed = await claim_next_job(
            session,
            worker_id=recovery_worker,
            lease_seconds=30,
        )

    assert reclaimed is not None
    assert reclaimed.id == job_id
    assert reclaimed.attempts == 2
    assert reclaimed.max_attempts == 3

    async with AsyncSessionFactory() as session:
        persisted = await session.get(BackgroundJob, job_id)
        assert persisted is not None
        assert persisted.status == "running"
        assert persisted.attempts == 2
        assert persisted.locked_by == recovery_worker
        assert persisted.locked_at is not None


@pytest.mark.asyncio
async def test_expired_worker_lease_at_max_attempts_moves_to_dead_letter() -> None:
    tenant = await _new_tenant()
    original_worker = f"exhausted-worker-{uuid4().hex}"
    async with AsyncSessionFactory() as session:
        job = await enqueue_job(
            session,
            tenant_id=tenant.id,
            job_type="telegram.accept_inbound",
            payload={"webhook_event_id": str(uuid4())},
            idempotency_key=f"lease-dead-letter:{uuid4()}",
            max_attempts=2,
        )
        job_id = job.id
        job.status = "running"
        job.attempts = 2
        job.locked_at = datetime.now(UTC) - timedelta(minutes=5)
        job.locked_by = original_worker
        await session.commit()

    async with AsyncSessionFactory() as session:
        reclaimed = await claim_next_job(
            session,
            worker_id=f"replacement-worker-{uuid4().hex}",
            lease_seconds=30,
        )
        assert reclaimed is None

    async with AsyncSessionFactory() as session:
        persisted = await session.get(BackgroundJob, job_id)
        assert persisted is not None
        assert persisted.status == "dead_letter"
        assert persisted.attempts == 2
        assert persisted.last_error_code == "worker_lease_expired"
        assert persisted.locked_at is None
        assert persisted.locked_by is None
