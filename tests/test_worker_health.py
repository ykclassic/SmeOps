import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from fastapi.testclient import TestClient

from app.db.session import AsyncSessionFactory
from app.main import app
from app.models import WorkerHeartbeat


def test_worker_health_requires_a_fresh_running_heartbeat():
    worker_id = f"health-test-{uuid4()}"

    async def write_heartbeat(last_seen_at):
        async with AsyncSessionFactory() as session:
            session.add(
                WorkerHeartbeat(
                    worker_id=worker_id,
                    status="running",
                    started_at=datetime.now(UTC) - timedelta(minutes=5),
                    last_seen_at=last_seen_at,
                )
            )
            await session.commit()

    async def cleanup():
        async with AsyncSessionFactory() as session:
            heartbeat = await session.get(WorkerHeartbeat, worker_id)
            if heartbeat is not None:
                await session.delete(heartbeat)
                await session.commit()

    try:
        asyncio.run(write_heartbeat(datetime.now(UTC) - timedelta(minutes=3)))
        with TestClient(app) as client:
            stale = client.get("/health/worker")
            assert stale.status_code == 503

        async def refresh_heartbeat():
            async with AsyncSessionFactory() as session:
                heartbeat = await session.get(WorkerHeartbeat, worker_id)
                heartbeat.last_seen_at = datetime.now(UTC)
                await session.commit()

        asyncio.run(refresh_heartbeat())
        with TestClient(app) as client:
            healthy = client.get("/health/worker")
        assert healthy.status_code == 200
        assert healthy.json()["worker"] == "running"
    finally:
        asyncio.run(cleanup())
