import sqlite3
from pathlib import Path

from agent_os.checkpoints import CHECKPOINT_DB_ENV
from agent_os.observations import SqliteObservationStore
from agent_os.permission_store import (
    InMemoryPermissionStore,
    PermissionRule,
    SqlitePermissionStore,
)
from agent_os.principal import Principal
from agent_os.stores import (
    ObservationStore,
    PermissionStore,
    RunStore,
    ScheduleStore,
    SqliteRunStore,
    SqliteScheduleStore,
    configure_sqlite_connection,
)


def test_storage_protocols_runtime_checkable():
    assert issubclass(SqliteRunStore, RunStore)
    assert issubclass(SqliteScheduleStore, ScheduleStore)
    assert issubclass(SqlitePermissionStore, PermissionStore)
    assert issubclass(InMemoryPermissionStore, PermissionStore)
    assert issubclass(SqliteObservationStore, ObservationStore)


def test_sqlite_run_store_operations(tmp_path: Path, monkeypatch):
    db_path = str(tmp_path / "checkpoints.sqlite")
    monkeypatch.setenv(CHECKPOINT_DB_ENV, db_path)

    store = SqliteRunStore()
    assert isinstance(store, RunStore)

    principal = Principal(id="local:tester", kind="human", display="Tester")
    run_id = store.create_run(
        "thread-store-1",
        "/workspace/store",
        "test storage seam",
        principal=principal,
    )

    run = store.get_run(run_id)
    assert run is not None
    assert run["created_by"] == "local:tester"
    assert run["workspace_id"] == "/workspace/store"

    seq = store.append_event(run_id, "status", {"status": "running"})
    assert seq == 1

    events = store.list_events(run_id)
    assert len(events) == 1

    # Approvals via store
    app_seq = store.record_approval(
        run_id,
        decision="approved",
        actor_id="local:tester",
        actor_kind="human",
        reason="Approved via store seam",
    )
    assert app_seq == 1

    approvals = store.list_approvals(run_id)
    assert len(approvals) == 1
    assert approvals[0]["decision"] == "approved"
    assert approvals[0]["actor_id"] == "local:tester"

    # Atomic cancellation via store
    cancel_success = store.record_cancellation_and_transition(
        run_id,
        actor_id="token:execution",
        actor_kind="service",
        reason="Aborted via store",
    )
    assert cancel_success is True
    assert store.get_run(run_id)["status"] == "cancelled"
    assert len(store.list_approvals(run_id)) == 2


def test_sqlite_schedule_store_operations(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("AGENT_OS_SCHED_DB", str(tmp_path / "sched.db"))

    store = SqliteScheduleStore()
    assert isinstance(store, ScheduleStore)

    sched_id = store.create_schedule(
        name="daily-health",
        kind="brief",
        trigger_kind="cron",
        trigger_value="0 8 * * *",
    )
    assert isinstance(sched_id, str)

    fetched = store.get_schedule(sched_id)
    assert fetched is not None
    assert fetched["name"] == "daily-health"

    listed = store.list_schedules()
    assert any(s["schedule_id"] == sched_id for s in listed)

    store.record_schedule_result(sched_id, status="completed")
    updated = store.get_schedule(sched_id)
    assert updated["last_status"] == "completed"

    assert store.remove_schedule(sched_id) is True
    assert store.get_schedule(sched_id) is None


def test_sqlite_permission_store_operations(tmp_path: Path):
    db_file = tmp_path / "perms.db"
    store = SqlitePermissionStore(str(db_file))
    assert isinstance(store, PermissionStore)

    rule = PermissionRule(
        permission_key="memory_write:write:markdown:append:notes.md",
        effect="always_approve",
        tier_at_creation="low",
        workspace_id="/custom/ws",
        taught_by="local:admin",
    )
    store.upsert(rule)

    fetched = store.get("memory_write:write:markdown:append:notes.md")
    assert fetched is not None
    assert fetched.effect == "always_approve"
    assert fetched.workspace_id == "/custom/ws"
    assert fetched.taught_by == "local:admin"

    rules = store.list()
    assert len(rules) == 1
    assert rules[0].taught_by == "local:admin"


def test_configure_sqlite_connection():
    conn = sqlite3.connect(":memory:")
    try:
        configure_sqlite_connection(conn, wal=True, busy_timeout_ms=15000)
        timeout_row = conn.execute("PRAGMA busy_timeout").fetchone()
        assert timeout_row[0] == 15000
    finally:
        conn.close()


def test_production_sqlite_paths_use_shared_connection_helper(monkeypatch, tmp_path: Path):
    import agent_os.stores.base as stores_base

    helper_calls: list[str] = []
    real_helper = stores_base.configure_sqlite_connection

    def tracking_helper(conn, *args, **kwargs):
        helper_calls.append(conn.__class__.__name__)
        return real_helper(conn, *args, **kwargs)

    monkeypatch.setattr(stores_base, "configure_sqlite_connection", tracking_helper)
    monkeypatch.setattr("agent_os.runs.configure_sqlite_connection", tracking_helper)
    monkeypatch.setattr("agent_os.permission_store.configure_sqlite_connection", tracking_helper)
    monkeypatch.setattr("agent_os.schedules.configure_sqlite_connection", tracking_helper)
    monkeypatch.setattr("agent_os.observations.configure_sqlite_connection", tracking_helper)

    # 1. Exercise runs _connect
    db_path = str(tmp_path / "checkpoints.sqlite")
    monkeypatch.setenv(CHECKPOINT_DB_ENV, db_path)
    from agent_os.runs import create_run
    create_run("th-helper-test", "/ws", "task")
    assert len(helper_calls) >= 1

    # 2. Exercise permission_store _connect
    helper_calls.clear()
    perm_db = str(tmp_path / "perms_track.db")
    perm_store = SqlitePermissionStore(perm_db)
    assert perm_store.list() == []
    assert len(helper_calls) >= 1

    # 3. Exercise observation store _connect
    helper_calls.clear()
    obs_db = str(tmp_path / "obs_track.db")
    obs_store = SqliteObservationStore(obs_db)
    assert obs_store.list(workspace_id="/test") == []
    assert len(helper_calls) >= 1


def test_api_and_scheduler_boundaries_use_store_interface(monkeypatch, tmp_path: Path):
    from agent_os.scheduler import dispatch_schedule
    from agent_os.stores import SqliteRunStore

    db_path = str(tmp_path / "checkpoints.sqlite")
    monkeypatch.setenv(CHECKPOINT_DB_ENV, db_path)
    monkeypatch.setenv("AGENT_OS_SCHED_DB", str(tmp_path / "sched.db"))

    # Direct store verification
    store = SqliteRunStore()
    run_id = store.create_run("th-boundary", "/ws", "boundary task")
    assert store.get_run(run_id)["status"] == "queued"

    # Scheduler dispatch through store
    schedule = {
        "schedule_id": "daily_check",
        "kind": "run",
        "payload": {"task": "scheduled work", "workspace": "/ws"},
    }

    import anyio

    async def run_dispatch():
        res = await dispatch_schedule(schedule)
        assert res.run_id is not None
        assert store.get_run(res.run_id) is not None

    anyio.run(run_dispatch)


def test_store_backend_discovery(monkeypatch):
    from agent_os.stores import list_store_backends, resolve_store_backend

    class FakeEntryPoint:
        def __init__(self, name, target):
            self.name = name
            self.target = target

        def load(self):
            return self.target

    class CustomPostgresRunStore:
        pass

    fake_eps = [FakeEntryPoint("postgres", CustomPostgresRunStore)]

    monkeypatch.setattr(
        "agent_os.plugins._get_entry_points",
        lambda group: fake_eps if group == "agent_os.stores" else [],
    )

    available = list_store_backends()
    assert available == ["postgres"]

    resolved = resolve_store_backend("postgres")
    assert resolved is CustomPostgresRunStore
