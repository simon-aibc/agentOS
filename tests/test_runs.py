import contextlib
import sqlite3
from pathlib import Path

import pytest

from agent_os.checkpoints import CHECKPOINT_DB_ENV
from agent_os.runs import (
    _get_db_path,
    _init_runs_db,
    append_event,
    create_run,
    get_run,
    list_events,
    list_runs,
    set_status,
)


@pytest.fixture
def runs_db(tmp_path, monkeypatch):
    monkeypatch.setenv(CHECKPOINT_DB_ENV, str(tmp_path / "checkpoints.db"))
    # The ledger lives in its own derived file, not the checkpoint DB.
    return Path(_get_db_path())


def test_init_runs_db_creates_tables(runs_db):
    _init_runs_db()

    with contextlib.closing(sqlite3.connect(runs_db)) as conn:
        cursor = conn.execute(
            """
            SELECT name FROM sqlite_master
            WHERE type = 'table' AND name IN ('runs', 'run_events')
            ORDER BY name
            """
        )
        assert [row[0] for row in cursor.fetchall()] == ["run_events", "runs"]


def test_run_events_use_monotonic_seq_under_interleaved_appends(runs_db):
    first_run = create_run("thread-1", "workspace-a", "first task")
    second_run = create_run("thread-2", "workspace-a", "second task")

    assert append_event(first_run, "status", {"status": "running"}) == 1
    assert append_event(second_run, "status", {"status": "running"}) == 1
    assert append_event(first_run, "node", {"name": "architect"}) == 2
    assert append_event(second_run, "result", {"ok": True}) == 2
    assert append_event(first_run, "interrupt", {"reason": "approval"}) == 3

    first_events = list_events(first_run)
    second_events = list_events(second_run)

    assert [event["seq"] for event in first_events] == [1, 2, 3]
    assert [event["seq"] for event in second_events] == [1, 2]
    assert [event["seq"] for event in list_events(first_run, after=1)] == [2, 3]
    assert first_events[1]["payload"] == {"name": "architect"}


def test_set_status_updates_transitions_and_terminal_fields(runs_db):
    run_id = create_run("thread-1", None, "task")

    set_status(run_id, "running")
    running = get_run(run_id)
    assert running is not None
    assert running["status"] == "running"
    assert running["ended_at"] is None
    assert running["error"] is None

    set_status(run_id, "interrupted")
    interrupted = get_run(run_id)
    assert interrupted is not None
    assert interrupted["status"] == "interrupted"
    assert interrupted["ended_at"] is None

    set_status(run_id, "error", error="boom", ended=True)
    failed = get_run(run_id)
    assert failed is not None
    assert failed["status"] == "error"
    assert failed["error"] == "boom"
    assert failed["ended_at"] is not None


def test_list_runs_supports_status_workspace_and_limit_filters(runs_db):
    first = create_run("thread-1", "workspace-a", "first")
    second = create_run("thread-2", "workspace-b", "second")
    third = create_run("thread-3", "workspace-a", "third")
    set_status(second, "running")
    set_status(third, "running")

    assert [run["run_id"] for run in list_runs(status="queued")] == [first]
    assert {run["run_id"] for run in list_runs(workspace="workspace-a")} == {
        first,
        third,
    }
    assert [
        run["run_id"] for run in list_runs(status="running", workspace="workspace-a")
    ] == [third]
    assert len(list_runs(limit=2)) == 2


def test_create_run_persists_principal_and_workspace_id(runs_db):
    from agent_os.principal import Principal

    principal = Principal(
        id="token:test-runner",
        kind="service",
        display="Service (test-runner)",
        on_behalf_of="operator-1",
    )
    run_id = create_run(
        "thread-p",
        None,
        "principal test task",
        workspace_id="/custom/workspace",
        principal=principal,
    )

    run = get_run(run_id)
    assert run is not None
    assert run["workspace_id"] == "/custom/workspace"
    assert run["workspace"] == "/custom/workspace"  # Back-compat
    assert run["created_by"] == "token:test-runner"
    assert run["created_by_kind"] == "service"


def test_run_approvals_ordered_decisions_and_audit(runs_db):
    from agent_os.runs import list_approvals, record_approval

    run_id = create_run("thread-audit", "/ws", "audit task")
    assert list_approvals(run_id) == []

    seq1 = record_approval(
        run_id,
        decision="approved",
        actor_id="local:simon",
        actor_kind="human",
        reason="Looks safe to execute",
    )
    assert seq1 == 1

    seq2 = record_approval(
        run_id,
        decision="approved",
        actor_id="token:auto-gate",
        actor_kind="service",
        on_behalf_of="lead-dev",
        reason="Second gate passed",
    )
    assert seq2 == 2

    seq3 = record_approval(
        run_id,
        decision="cancelled",
        actor_id="local:simon",
        actor_kind="human",
        reason="User requested cancellation",
    )
    assert seq3 == 3

    history = list_approvals(run_id)
    assert len(history) == 3
    assert [h["seq"] for h in history] == [1, 2, 3]
    assert [h["decision"] for h in history] == ["approved", "approved", "cancelled"]
    assert history[0]["actor_id"] == "local:simon"
    assert history[1]["actor_id"] == "token:auto-gate"
    assert history[1]["on_behalf_of"] == "lead-dev"


def test_record_approval_and_transition_atomic_and_prevents_false_audits(runs_db):
    from agent_os.runs import list_approvals, record_approval_and_transition

    run_id = create_run("thread-gate", "/ws", "gate task")
    set_status(run_id, "interrupted")

    # 1. Successful transition from interrupted -> running
    success = record_approval_and_transition(
        run_id,
        decision="approved",
        actor_id="local:operator",
        actor_kind="human",
        reason="Approved by user",
        expected="interrupted",
        status="running",
    )
    assert success is True
    assert get_run(run_id)["status"] == "running"
    approvals = list_approvals(run_id)
    assert len(approvals) == 1
    assert approvals[0]["decision"] == "approved"

    # 2. Conflicting / duplicate approval attempt on already running run -> must fail and NOT insert false audit row
    failed_attempt = record_approval_and_transition(
        run_id,
        decision="approved",
        actor_id="local:another_user",
        actor_kind="human",
        expected="interrupted",
        status="running",
    )
    assert failed_attempt is False
    # Approvals list must still be only 1!
    assert len(list_approvals(run_id)) == 1


def test_record_cancellation_and_transition_atomic(runs_db):
    from agent_os.runs import list_approvals, record_cancellation_and_transition

    run_id = create_run("thread-cancel-atomic", "/ws", "cancel atomic task")
    set_status(run_id, "running")

    success = record_cancellation_and_transition(
        run_id,
        actor_id="token:execution",
        actor_kind="service",
        reason="Operator requested abort",
    )
    assert success is True

    run = get_run(run_id)
    assert run["status"] == "cancelled"
    assert run["ended_at"] is not None

    approvals = list_approvals(run_id)
    assert len(approvals) == 1
    assert approvals[0]["seq"] == 1
    assert approvals[0]["decision"] == "cancelled"
    assert approvals[0]["actor_id"] == "token:execution"
    assert approvals[0]["actor_kind"] == "service"
    assert approvals[0]["reason"] == "Operator requested abort"


def test_cancellation_conflict_creates_no_audit_decision(runs_db):
    from agent_os.runs import list_approvals, record_cancellation_and_transition

    run_id = create_run("thread-cancel-conflict", "/ws", "terminal task")
    set_status(run_id, "completed", ended=True)

    # Attempting to cancel already-completed run
    success = record_cancellation_and_transition(
        run_id,
        actor_id="token:execution",
        actor_kind="service",
    )
    assert success is False
    assert get_run(run_id)["status"] == "completed"
    assert list_approvals(run_id) == []


def test_audit_attempt_against_unknown_run_rejected(runs_db):
    import pytest

    from agent_os.runs import (
        list_approvals,
        record_approval,
        record_approval_and_transition,
        record_cancellation_and_transition,
    )

    with pytest.raises(KeyError, match="Run 'nonexistent-run' not found"):
        record_approval(
            "nonexistent-run",
            decision="approved",
            actor_id="local:tester",
            actor_kind="human",
        )

    assert not record_approval_and_transition(
        "nonexistent-run",
        actor_id="local:tester",
        actor_kind="human",
    )

    assert not record_cancellation_and_transition(
        "nonexistent-run",
        actor_id="local:tester",
        actor_kind="human",
    )

    assert list_approvals("nonexistent-run") == []
