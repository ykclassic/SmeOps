import asyncio
from types import SimpleNamespace

import pytest

from app.main import app


@pytest.mark.asyncio
async def test_staging_lifespan_starts_and_stops_embedded_worker(monkeypatch):
    events = []

    async def fake_worker(stop_event):
        events.append("started")
        await stop_event.wait()
        events.append("stopped")

    async def fake_dispose():
        events.append("disposed")

    monkeypatch.setattr("app.main.settings.app_env", "staging")
    monkeypatch.setattr("app.main.run_embedded_worker", fake_worker)
    monkeypatch.setattr(
        "app.main.engine",
        SimpleNamespace(dispose=fake_dispose),
    )

    async with app.router.lifespan_context(app):
        await asyncio.sleep(0)
        assert events == ["started"]

    assert events == ["started", "stopped", "disposed"]


@pytest.mark.asyncio
async def test_test_environment_does_not_start_embedded_worker(monkeypatch):
    events = []

    async def fake_worker(stop_event):
        events.append("started")

    async def fake_dispose():
        events.append("disposed")

    monkeypatch.setattr("app.main.settings.app_env", "test")
    monkeypatch.setattr("app.main.run_embedded_worker", fake_worker)
    monkeypatch.setattr(
        "app.main.engine",
        SimpleNamespace(dispose=fake_dispose),
    )

    async with app.router.lifespan_context(app):
        await asyncio.sleep(0)

    assert events == ["disposed"]
