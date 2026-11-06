import asyncio

import pytest

from app.worker import run_embedded_worker


@pytest.mark.asyncio
async def test_only_one_embedded_worker_holds_the_postgres_lock(monkeypatch):
    first_stop = asyncio.Event()
    second_stop = asyncio.Event()
    first_started = asyncio.Event()
    worker_calls = []

    async def fake_run_forever(*, worker_id, handlers, stop_event):
        worker_calls.append(worker_id)
        first_started.set()
        await stop_event.wait()

    monkeypatch.setattr("app.worker.run_forever", fake_run_forever)

    first = asyncio.create_task(run_embedded_worker(first_stop))
    await asyncio.wait_for(first_started.wait(), timeout=5)

    second = asyncio.create_task(run_embedded_worker(second_stop))
    await asyncio.sleep(0.2)

    assert len(worker_calls) == 1

    second_stop.set()
    first_stop.set()
    await asyncio.wait_for(asyncio.gather(first, second), timeout=5)

    assert len(worker_calls) == 1
