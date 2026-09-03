"""Tests for the schedule Runtime API (r1.8a.5)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agent_os.checkpoints import CHECKPOINT_DB_ENV
from agent_os.schedules import SCHED_DB_ENV


@pytest.fixture(autouse=True)
def _isolate_db(tmp_path, monkeypatch):
    monkeypatch.setenv(CHECKPOINT_DB_ENV, str(tmp_path / "checkpoints.db"))
    monkeypatch.delenv(SCHED_DB_ENV, raising=False)
    # Disable the scheduler loop during tests.
    monkeypatch.setenv("AGENT_OS_SCHEDULER_ENABLED", "false")


@pytest.fixture
def client():
    from agent_os.server.api import app

    with TestClient(app) as c:
        yield c


# ── POST /api/schedules ─────────────────────────────────────────────


def test_create_schedule_run(client: TestClient):
    resp = client.post(
        "/api/schedules",
        json={
            "name": "daily-run",
            "kind": "run",
            "cron": "0 9 * * *",
            "task": "refactor code",
        },
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "daily-run"
    assert data["kind"] == "run"
    assert data["trigger_kind"] == "cron"
    assert data["next_run_at"] is not None
    assert data["payload"]["task"] == "refactor code"
    assert data["enabled"] is True


def test_create_schedule_brief_interval(client: TestClient):
    resp = client.post(
        "/api/schedules",
        json={
            "name": "hourly-brief",
            "kind": "brief",
            "every": "1h",
        },
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["kind"] == "brief"
    assert data["trigger_kind"] == "interval"


def test_create_schedule_422_both_cron_and_every(client: TestClient):
    resp = client.post(
        "/api/schedules",
        json={
            "name": "bad",
            "kind": "run",
            "cron": "0 9 * * *",
            "every": "1h",
            "task": "t",
        },
    )
    assert resp.status_code == 422


def test_create_schedule_422_missing_trigger(client: TestClient):
    resp = client.post(
        "/api/schedules",
        json={
            "name": "bad",
            "kind": "run",
            "task": "t",
        },
    )
    assert resp.status_code == 422


def test_create_schedule_422_run_without_task(client: TestClient):
    resp = client.post(
        "/api/schedules",
        json={
            "name": "bad",
            "kind": "run",
            "every": "1h",
        },
    )
    assert resp.status_code == 422


def test_create_schedule_422_brief_with_task(client: TestClient):
    resp = client.post(
        "/api/schedules",
        json={
            "name": "bad",
            "kind": "brief",
            "every": "1h",
            "task": "should fail",
        },
    )
    assert resp.status_code == 422


# ── GET /api/schedules ──────────────────────────────────────────────


def test_list_schedules_empty(client: TestClient):
    resp = client.get("/api/schedules")
    assert resp.status_code == 200
    assert resp.json() == []


def test_list_schedules_returns_created(client: TestClient):
    client.post(
        "/api/schedules",
        json={
            "name": "r1",
            "kind": "run",
            "every": "30m",
            "task": "t1",
        },
    )
    client.post(
        "/api/schedules",
        json={
            "name": "b1",
            "kind": "brief",
            "every": "24h",
        },
    )

    resp = client.get("/api/schedules")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 2


def test_list_schedules_filter_by_kind(client: TestClient):
    client.post(
        "/api/schedules",
        json={
            "name": "r1",
            "kind": "run",
            "every": "30m",
            "task": "t1",
        },
    )
    client.post(
        "/api/schedules",
        json={
            "name": "b1",
            "kind": "brief",
            "every": "24h",
        },
    )
    client.post(
        "/api/schedules",
        json={
            "name": "c1",
            "kind": "app_callback",
            "every": "10m",
            "url": "https://example.com/webhook",
        },
    )

    resp = client.get("/api/schedules", params={"kind": "run"})
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["kind"] == "run"

    resp = client.get("/api/schedules", params={"kind": "app_callback"})
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["kind"] == "app_callback"


def test_create_schedule_app_callback(client: TestClient):
    resp = client.post(
        "/api/schedules",
        json={
            "name": "hook",
            "kind": "app_callback",
            "every": "1h",
            "url": "https://api.example.com/webhook",
            "method": "post",
            "headers": {"X-Custom": "header-value"},
            "body": {"event": "start"},
        },
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "hook"
    assert data["kind"] == "app_callback"
    assert data["trigger_kind"] == "interval"
    assert data["payload"]["url"] == "https://api.example.com/webhook"
    assert data["payload"]["method"] == "POST"
    assert data["payload"]["headers"] == {"X-Custom": "header-value"}
    assert data["payload"]["body"] == {"event": "start"}


def test_create_schedule_422_app_callback_without_url(client: TestClient):
    resp = client.post(
        "/api/schedules",
        json={
            "name": "bad",
            "kind": "app_callback",
            "every": "1h",
        },
    )
    assert resp.status_code == 422


def test_create_schedule_422_app_callback_with_task(client: TestClient):
    resp = client.post(
        "/api/schedules",
        json={
            "name": "bad",
            "kind": "app_callback",
            "every": "1h",
            "url": "https://example.com/webhook",
            "task": "should fail",
        },
    )
    assert resp.status_code == 422


# ── Existing v1.7 routes preserved ─────────────────────────────────


def test_health_check_preserved(client: TestClient):
    resp = client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_runs_endpoint_preserved(client: TestClient):
    resp = client.get("/api/runs")
    assert resp.status_code == 200


# ── Disabled scheduler mode ─────────────────────────────────────────


def test_crud_available_with_scheduler_disabled(client: TestClient):
    """CRUD routes must work even when the scheduler loop is disabled."""
    resp = client.post(
        "/api/schedules",
        json={
            "name": "test",
            "kind": "run",
            "every": "1h",
            "task": "t1",
        },
    )
    assert resp.status_code == 201

    resp = client.get("/api/schedules")
    assert resp.status_code == 200
    assert len(resp.json()) == 1


# ── Review Regression Tests ─────────────────────────────────────────


def test_create_schedule_422_extra_fields(client: TestClient):
    """ScheduleInput must forbid unknown fields."""
    resp = client.post(
        "/api/schedules",
        json={
            "name": "no-extras",
            "kind": "run",
            "every": "1h",
            "task": "t1",
            "unexpected": "silently ignored",
        },
    )
    assert resp.status_code == 422


def test_create_schedule_422_whitespace_only(client: TestClient):
    """ScheduleInput must reject whitespace-only names and tasks."""
    resp = client.post(
        "/api/schedules",
        json={
            "name": "   ",
            "kind": "run",
            "every": "1h",
            "task": "t1",
        },
    )
    assert resp.status_code == 422

    resp = client.post(
        "/api/schedules",
        json={
            "name": "valid",
            "kind": "run",
            "every": "1h",
            "task": "   \n\t",
        },
    )
    assert resp.status_code == 422
