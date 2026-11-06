import asyncio

import pytest

from app.jobs.worker import run_forever


@pytest.mark.asyncio
async def test_worker_stops_after_current_job_iteration(monkeypatch):
    stop_event = asyncio.Event()
    calls = 0
    heartbeat_states = []

    async def fake_heartbeat(worker_id, *, stopped=False):
        heartbeat_states.append(stopped)

    async def fake_run_once(*, worker_id, handlers):
        nonlocal calls
        calls += 1
        stop_event.set()
        return True

    monkeypatch.setattr("app.jobs.worker.run_once", fake_run_once)
    monkeypatch.setattr("app.jobs.worker._safe_worker_heartbeat", fake_heartbeat)

    await run_forever(
        worker_id="test-worker",
        handlers={},
        stop_event=stop_event,
    )

    assert calls == 1
    assert heartbeat_states == [False, True]


@pytest.mark.asyncio
async def test_worker_marks_heartbeat_stopped_when_cancelled(monkeypatch):
    entered_handler = asyncio.Event()
    release_handler = asyncio.Event()
    heartbeat_states = []

    async def fake_heartbeat(worker_id, *, stopped=False):
        heartbeat_states.append(stopped)

    async def fake_run_once(*, worker_id, handlers):
        entered_handler.set()
        await release_handler.wait()
        return True

    monkeypatch.setattr("app.jobs.worker.run_once", fake_run_once)
    monkeypatch.setattr("app.jobs.worker._safe_worker_heartbeat", fake_heartbeat)

    task = asyncio.create_task(
        run_forever(worker_id="cancelled-worker", handlers={})
    )
    await asyncio.wait_for(entered_handler.wait(), timeout=1)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert heartbeat_states == [False, True]
