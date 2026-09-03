"""Tests for the FastAPI lifespan / scheduler integration (r1.8a.5)."""

from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent_os.checkpoints import CHECKPOINT_DB_ENV
from agent_os.schedules import SCHED_DB_ENV


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv(CHECKPOINT_DB_ENV, str(tmp_path / "checkpoints.db"))
    monkeypatch.delenv(SCHED_DB_ENV, raising=False)


def test_lifespan_starts_and_stops_scheduler(monkeypatch):
    """The scheduler service starts during app startup and stops on shutdown."""
    monkeypatch.setenv("AGENT_OS_SCHEDULER_ENABLED", "true")
    monkeypatch.setenv("AGENT_OS_SCHED_TICK_SECONDS", "60")  # slow tick

    from agent_os.server.api import app

    with TestClient(app) as client:
        sched = app.state.scheduler
        assert sched is not None
        assert sched._running is True

        # Health check works while scheduler is running.
        resp = client.get("/api/health")
        assert resp.status_code == 200

    # After context exit, the scheduler should be stopped.
    assert sched._running is False


def test_lifespan_no_loop_when_disabled(monkeypatch):
    monkeypatch.setenv("AGENT_OS_SCHEDULER_ENABLED", "false")

    from agent_os.server.api import app

    with TestClient(app):
        sched = app.state.scheduler
        assert sched is not None
        # The loop task should not be started.
        assert sched._loop_task is None


def test_import_does_not_start_loop(monkeypatch):
    """Importing the app module must not start a scheduler loop."""
    monkeypatch.setenv("AGENT_OS_SCHEDULER_ENABLED", "true")
    # Fresh import without TestClient __enter__ should not start anything.
    from agent_os.server.api import app

    # The app exists but without entering the lifespan, no scheduler state.
    assert not hasattr(app.state, "scheduler") or app.state.scheduler._loop_task is None


def test_lifespan_exception_cleanup(monkeypatch):
    """If the lifespan yield raises, the scheduler must still be stopped."""
    monkeypatch.setenv("AGENT_OS_SCHEDULER_ENABLED", "true")

    from agent_os.server.api import _lifespan

    async def run_failing_lifespan():
        app = FastAPI()
        try:
            async with _lifespan(app):
                assert app.state.scheduler._running is True
                raise RuntimeError("Lifespan crash")
        except RuntimeError:
            pass
        # The finally block should have stopped it.
        assert app.state.scheduler._running is False

    asyncio.run(run_failing_lifespan())
