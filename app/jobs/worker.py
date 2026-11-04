import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime

from app.db.session import AsyncSessionFactory
from app.jobs.queue import (
    ClaimedJob,
    JobLeaseLost,
    claim_next_job,
    complete_job,
    fail_job,
)

logger = logging.getLogger(__name__)
JobHandler = Callable[[ClaimedJob], Awaitable[None]]


async def run_once(
    *,
    worker_id: str,
    handlers: Mapping[str, JobHandler],
) -> bool:
    async with AsyncSessionFactory() as session:
        job = await claim_next_job(session, worker_id=worker_id)

    if job is None:
        return False

    handler = handlers.get(job.job_type)
    if handler is None:
        error_code = "handler_not_registered"
    else:
        try:
            await handler(job)
        except Exception as exc:
            logger.error(
                "Background job handler raised an exception",
                extra={
                    "job_id": str(job.id),
                    "job_type": job.job_type,
                    "error_type": type(exc).__name__,
                },
            )
            error_code = "handler_failed"
        else:
            async with AsyncSessionFactory() as session:
                try:
                    await complete_job(session, job_id=job.id, worker_id=worker_id)
                except JobLeaseLost:
                    logger.warning(
                        "Background job completion rejected because lease was lost",
                        extra={"job_id": str(job.id), "job_type": job.job_type},
                    )
            return True

    async with AsyncSessionFactory() as session:
        try:
            await fail_job(
                session,
                job_id=job.id,
                worker_id=worker_id,
                error_code=error_code,
            )
        except JobLeaseLost:
            logger.warning(
                "Background job failure update rejected because lease was lost",
                extra={"job_id": str(job.id), "job_type": job.job_type},
            )
    return True


async def _record_worker_heartbeat(worker_id: str, *, stopped: bool = False) -> None:
    now = datetime.now(UTC)
    async with AsyncSessionFactory() as session:
        if stopped:
            await session.execute(
                update(WorkerHeartbeat)
                .where(WorkerHeartbeat.worker_id == worker_id)
                .values(status="stopped", last_seen_at=now, stopped_at=now)
            )
        else:
            statement = (
                insert(WorkerHeartbeat)
                .values(
                    worker_id=worker_id,
                    status="running",
                    started_at=now,
                    last_seen_at=now,
                    stopped_at=None,
                )
                .on_conflict_do_update(
                    index_elements=[WorkerHeartbeat.worker_id],
                    set_={
                        "status": "running",
                        "last_seen_at": now,
                        "stopped_at": None,
                    },
                )
            )
            await session.execute(statement)
        await session.commit()


async def _safe_worker_heartbeat(worker_id: str, *, stopped: bool = False) -> None:
    try:
        await _record_worker_heartbeat(worker_id, stopped=stopped)
    except Exception as exc:
        logger.error(
            "Worker heartbeat persistence failed",
            extra={"worker_id": worker_id, "error_type": type(exc).__name__},
        )


async def run_forever(
    *,
    worker_id: str,
    handlers: Mapping[str, JobHandler],
    poll_interval_seconds: float = 1.0,
    stop_event: asyncio.Event | None = None,
    heartbeat_interval_seconds: float = 20.0,
) -> None:
    if poll_interval_seconds <= 0:
        raise ValueError("poll_interval_seconds must be positive")
    if heartbeat_interval_seconds < 5:
        raise ValueError("heartbeat_interval_seconds must be at least 5")

    loop = asyncio.get_running_loop()
    last_heartbeat = 0.0
    await _safe_worker_heartbeat(worker_id)
    while stop_event is None or not stop_event.is_set():
        if loop.time() - last_heartbeat >= heartbeat_interval_seconds:
            await _safe_worker_heartbeat(worker_id)
            last_heartbeat = loop.time()
        did_work = await run_once(worker_id=worker_id, handlers=handlers)
        if not did_work:
            if stop_event is None:
                await asyncio.sleep(poll_interval_seconds)
            else:
                try:
                    await asyncio.wait_for(
                        stop_event.wait(),
                        timeout=poll_interval_seconds,
                    )
                except TimeoutError:
                    pass
\n    await _safe_worker_heartbeat(worker_id, stopped=True)\n