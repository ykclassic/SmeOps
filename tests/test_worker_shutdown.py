import asyncio

import pytest

from app.jobs.worker import run_forever


@pytest.mark.asyncio
async def test_worker_stops_after_current_job_iteration(monkeypatch):
    stop_event = asyncio.Event()
    calls = 0

    async def fake_run_once(*, worker_id, handlers):
        nonlocal calls
        calls += 1
        stop_event.set()
        return True

    monkeypatch.setattr("app.jobs.worker.run_once", fake_run_once)

    await run_forever(
        worker_id="test-worker",
        handlers={},
        stop_event=stop_event,
    )

    assert calls == 1
