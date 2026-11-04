from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db_session
from app.models import WorkerHeartbeat

router = APIRouter(prefix="/health", tags=["health"])


@router.get("/live", status_code=status.HTTP_200_OK)
async def liveness() -> dict[str, str]:
    """Indicate that the API process is running; dependency health is not implied."""
    return {"status": "ok"}


@router.get("/ready", status_code=status.HTTP_200_OK)
async def readiness(
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> dict[str, str]:
    """Return ready only when the configured database can answer a trivial query."""
    try:
        await session.execute(text("SELECT 1"))
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database unavailable",
        ) from exc
    return {"status": "ready", "database": "ok"}

@router.get("/worker", status_code=status.HTTP_200_OK)
async def worker_health(
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> dict[str, str]:
    """Report worker health only when a running worker has a fresh heartbeat."""
    result = await session.execute(
        select(WorkerHeartbeat)
        .where(WorkerHeartbeat.status == "running")
        .order_by(WorkerHeartbeat.last_seen_at.desc())
        .limit(1)
    )
    heartbeat = result.scalar_one_or_none()
    if (
        heartbeat is None
        or heartbeat.last_seen_at < datetime.now(UTC) - timedelta(seconds=90)
    ):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Background worker unavailable",
        )
    return {
        "status": "ok",
        "worker": "running",
        "last_seen_at": heartbeat.last_seen_at.isoformat(),
    }
