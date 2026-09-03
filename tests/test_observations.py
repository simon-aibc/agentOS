import contextlib
import io
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from rich.console import Console

from agent_os.cli.app import async_main
from agent_os.observations import (
    MAX_ADVISORIES,
    ObservationValidationError,
    SqliteObservationStore,
    observation_workspace_id,
    open_observation_store,
    render_advisory_context,
    task_signature_for_input,
)
from agent_os.server.api import EXECUTION_TOKEN_ENV, app


def _store(path: Path) -> SqliteObservationStore:
    return SqliteObservationStore(str(path))


def _record(
    store: SqliteObservationStore, workspace: str, *, kind: str = "memory_write"
):
    return store.create(
        workspace_id=workspace,
        run_id="run-1",
        thread_id="thread-1",
        task_kind=kind,
        approach="native_tool:memory_write",
        outcome_signal="unknown",
        outcome_evidence="terminal_status=completed",
        source="server.run",
    )


def test_observations_persist_and_require_explicit_outcomes(tmp_path: Path) -> None:
    path = tmp_path / "observations.db"
    store = _store(path)
    observation = _record(store, "workspace-a")

    reloaded = _store(path).get(observation.observation_id)
    assert reloaded is not None
    assert reloaded.schema_version == 1
    assert reloaded.outcome_signal == "unknown"
    assert reloaded.outcome_evidence == "terminal_status=completed"

    updated = store.record_outcome(
        observation.observation_id,
        signal="edited",
        evidence="Operator adjusted the delivered artifact.",
    )
    assert updated is not None
    assert updated.outcome_signal == "edited"
    assert updated.outcome_evidence == "Operator adjusted the delivered artifact."


def test_task_signature_hashes_exact_submitted_task_and_persists_for_new_writes(
    tmp_path: Path,
) -> None:
    first_task = "Prepare weekly report for leadership"
    different_task = "Prepare weekly report for leadership\nContext: finance"
    signature = task_signature_for_input(first_task)

    assert signature is not None
    assert signature != task_signature_for_input(different_task)
    assert "weekly report" not in signature

    store = _store(tmp_path / "observations.db")
    observation = store.create(
        workspace_id="workspace-a",
        run_id="run-signature",
        thread_id="thread-signature",
        task_kind="workflow",
        task_signature=signature,
        approach="default-v1",
        source="server.run",
    )

    assert (
        _store(tmp_path / "observations.db").get(observation.observation_id)
        == observation
    )
    assert observation.task_signature == signature


def test_existing_observations_table_migrates_task_signature_additively(
    tmp_path: Path,
) -> None:
    import sqlite3

    db_path = tmp_path / "legacy-observations.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE observations (
            observation_id TEXT PRIMARY KEY, schema_version INTEGER NOT NULL,
            workspace_id TEXT NOT NULL, run_id TEXT, thread_id TEXT,
            task_kind TEXT NOT NULL, approach TEXT NOT NULL, artifact_refs TEXT NOT NULL,
            outcome_signal TEXT NOT NULL, outcome_evidence TEXT, source TEXT NOT NULL,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        )
        """
    )
    conn.commit()
    conn.close()

    _store(db_path)
    with contextlib.closing(sqlite3.connect(db_path)) as migrated:
        columns = {
            row[1] for row in migrated.execute("PRAGMA table_info(observations)")
        }
    assert "task_signature" in columns


def test_observation_validation_and_workspace_isolation(tmp_path: Path) -> None:
    store = _store(tmp_path / "observations.db")
    first = _record(store, "workspace-a")
    _record(store, "workspace-b")

    assert [item.observation_id for item in store.list(workspace_id="workspace-a")] == [
        first.observation_id
    ]
    try:
        store.record_outcome(first.observation_id, signal="unknown", evidence=None)
    except ObservationValidationError as exc:
        assert "accepted, rejected, or edited" in str(exc)
    else:
        raise AssertionError("unknown must not be an operator-recorded outcome")


def test_observation_store_open_is_fail_safe(monkeypatch) -> None:
    def broken_store(path: str):
        raise OSError(f"cannot open {path}")

    monkeypatch.setattr("agent_os.observations.SqliteObservationStore", broken_store)

    assert open_observation_store() is None


def test_advisory_retrieval_is_bounded_relevant_and_not_unknown(tmp_path: Path) -> None:
    store = _store(tmp_path / "observations.db")
    for index in range(MAX_ADVISORIES + 2):
        record = _record(store, "workspace-a")
        store.record_outcome(
            record.observation_id, signal="accepted", evidence=f"review {index}"
        )
    unknown = _record(store, "workspace-a")
    other_kind = _record(store, "workspace-a", kind="workflow")
    store.record_outcome(
        other_kind.observation_id, signal="rejected", evidence="not relevant"
    )

    context = render_advisory_context(
        store,
        workspace_id="workspace-a",
        task_kind="memory_write",
    )
    assert context.count("Historical evidence only") == MAX_ADVISORIES
    assert "advisory only" in context
    assert unknown.observation_id not in context
    assert "not relevant" not in context


def test_runtime_initial_state_injects_fixed_strategy_not_raw_evidence(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from agent_os.server import runtime

    database = tmp_path / "observations.db"
    monkeypatch.setenv("AGENT_OS_OBSERVATIONS_DB", str(database))
    monkeypatch.setattr(runtime, "composed_workspace", lambda: None)
    store = _store(database)
    record = _record(store, "standalone")
    store.record_outcome(
        record.observation_id, signal="edited", evidence="Operator refined it"
    )

    state = runtime.initial_state("private task text", run_id="run-strategy")

    assert state["observation_context"] is None
    hint = state["strategy_hint"]
    assert hint is not None
    assert "Operator refined it" not in hint.directive
    assert "private task text" not in hint.directive


@pytest.mark.anyio
async def test_observations_cli_lists_and_records_workspace_outcome(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace_path = workspace / "workspace.toml"
    workspace_path.write_text('[workspace]\nname = "observations"\n', encoding="utf-8")
    monkeypatch.delenv("AGENT_OS_OBSERVATIONS_DB", raising=False)
    store = _store(workspace / "observations.db")
    record = _record(store, observation_workspace_id(workspace))
    output = io.StringIO()
    console = Console(file=output, force_terminal=False, color_system=None)

    listed = await async_main(
        ["observations", "list", "--workspace", str(workspace_path)], console=console
    )
    recorded = await async_main(
        [
            "observations",
            "record-outcome",
            record.observation_id,
            "--signal",
            "accepted",
            "--evidence",
            "Reviewed by operator",
            "--workspace",
            str(workspace_path),
        ],
        console=console,
    )
    assert listed == 0
    assert recorded == 0
    assert store.get(record.observation_id).outcome_signal == "accepted"
    assert record.observation_id in output.getvalue()


def test_observations_api_uses_private_auth_and_updates_outcome(
    tmp_path: Path,
    monkeypatch,
) -> None:
    database = tmp_path / "observations.db"
    monkeypatch.setenv("AGENT_OS_OBSERVATIONS_DB", str(database))
    monkeypatch.setenv(EXECUTION_TOKEN_ENV, "observation-token")
    store = _store(database)
    record = _record(store, "standalone")
    client = TestClient(app)

    assert client.get("/api/observations").status_code == 403
    headers = {"X-Execution-Token": "observation-token"}
    listed = client.get("/api/observations", headers=headers)
    assert listed.status_code == 200
    assert [item["observation_id"] for item in listed.json()] == [record.observation_id]

    updated = client.post(
        f"/api/observations/{record.observation_id}/outcome",
        headers=headers,
        json={"signal": "rejected", "evidence": "Operator rejected result"},
    )
    assert updated.status_code == 200
    assert updated.json()["outcome_signal"] == "rejected"


def test_migration_from_v220_schema(tmp_path: Path) -> None:
    import sqlite3

    db_path = tmp_path / "v220_observations.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        CREATE TABLE strategy_assignments (
            run_id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL,
            task_kind TEXT NOT NULL,
            strategy_id TEXT NOT NULL,
            selected_at TEXT NOT NULL,
            UNIQUE (workspace_id, task_kind, run_id)
        )
        """
    )
    conn.execute(
        """
        INSERT INTO strategy_assignments (run_id, workspace_id, task_kind, strategy_id, selected_at)
        VALUES ('v220-run', 'ws-test', 'workflow', 'verification-first-v1', '2026-08-18T10:00:00Z')
        """
    )
    conn.commit()
    conn.close()

    store = _store(db_path)
    assignment = store.get_strategy_assignment("v220-run")
    assert assignment is not None
    assert assignment.run_id == "v220-run"
    assert assignment.workspace_id == "ws-test"
    assert assignment.task_kind == "workflow"
    assert assignment.strategy_id == "verification-first-v1"
    assert assignment.strategy_version == 1
    assert assignment.selection_reason == "default"
    assert assignment.selector_version == "v1.0"
    assert assignment.evidence_summary is None
    assert assignment.selected_at == "2026-08-18T10:00:00Z"


def test_strategy_assignment_crud_and_workspace_isolation(tmp_path: Path) -> None:
    store = _store(tmp_path / "observations.db")
    summary = {
        "labelled_counts": {"default-v1": 5, "verification-first-v1": 5},
        "scores": {"default-v1": 0.4, "verification-first-v1": 0.8},
        "winner": "verification-first-v1",
        "threshold_met": True,
        "margin": 0.4,
    }
    store.assign_strategy(
        workspace_id="workspace-a",
        task_kind="workflow",
        run_id="run-a",
        strategy_id="verification-first-v1",
        strategy_version=1,
        selection_reason="evidence_backed",
        selector_version="v1.0",
        evidence_summary=summary,
    )

    assignment = store.get_strategy_assignment("run-a")
    assert assignment is not None
    assert assignment.run_id == "run-a"
    assert assignment.workspace_id == "workspace-a"
    assert assignment.selection_reason == "evidence_backed"
    assert assignment.evidence_summary == summary
    assert assignment.to_dict()["strategy_id"] == "verification-first-v1"

    assert store.get_strategy_assignment("nonexistent-run") is None


def test_strategy_assignment_evidence_summary_rejects_extra_keys(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path / "observations.db")
    assignment = {
        "workspace_id": "workspace-a",
        "task_kind": "workflow",
        "run_id": "run-summary",
        "strategy_id": "verification-first-v1",
    }

    with pytest.raises(ObservationValidationError, match="must contain exactly"):
        store.assign_strategy(**assignment, evidence_summary={"task": "raw text"})


def test_strategy_assignment_evidence_summary_rejects_wrong_key_set(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path / "observations.db")
    assignment = {
        "workspace_id": "workspace-a",
        "task_kind": "workflow",
        "run_id": "run-summary",
        "strategy_id": "verification-first-v1",
    }

    with pytest.raises(ObservationValidationError, match="must contain exactly"):
        store.assign_strategy(
            **assignment,
            evidence_summary={"labelled_counts": {}, "extra_field": "bad"},
        )


def test_strategy_assignment_evidence_summary_accepts_full_shape(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path / "observations.db")
    assignment = {
        "workspace_id": "workspace-a",
        "task_kind": "workflow",
        "run_id": "run-summary",
        "strategy_id": "verification-first-v1",
    }
    summary = {
        "labelled_counts": {"default-v1": 5, "verification-first-v1": 6},
        "scores": {"default-v1": 0.4, "verification-first-v1": 0.8},
        "winner": "verification-first-v1",
        "threshold_met": True,
        "margin": 0.4,
    }
    store.assign_strategy(**assignment, evidence_summary=summary)
    stored = store.get_strategy_assignment("run-summary")
    assert stored is not None
    assert stored.evidence_summary == summary


@pytest.mark.anyio
async def test_observations_cli_assignment_subcommand(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace_path = workspace / "workspace.toml"
    workspace_path.write_text('[workspace]\nname = "observations"\n', encoding="utf-8")
    monkeypatch.delenv("AGENT_OS_OBSERVATIONS_DB", raising=False)
    store = _store(workspace / "observations.db")
    ws_id = observation_workspace_id(workspace)
    summary = {
        "labelled_counts": {"default-v1": 7, "verification-first-v1": 6},
        "scores": {"default-v1": 0.43, "verification-first-v1": 0.83},
        "winner": "verification-first-v1",
        "threshold_met": True,
        "margin": 0.40,
    }
    store.assign_strategy(
        workspace_id=ws_id,
        task_kind="workflow",
        run_id="run-audit-cli",
        strategy_id="verification-first-v1",
        strategy_version=1,
        selection_reason="evidence_backed",
        selector_version="v1.0",
        evidence_summary=summary,
    )

    output = io.StringIO()
    console = Console(file=output, force_terminal=False, color_system=None)

    code = await async_main(
        [
            "observations",
            "assignment",
            "run-audit-cli",
            "--workspace",
            str(workspace_path),
        ],
        console=console,
    )
    assert code == 0
    text = output.getvalue()
    assert "run-audit-cli" in text
    assert "evidence_backed" in text
    assert "verification-first-v1" in text

    # Missing run returns 1
    err_output = io.StringIO()
    err_console = Console(file=err_output, force_terminal=False, color_system=None)
    missing_code = await async_main(
        [
            "observations",
            "assignment",
            "unknown-run",
            "--workspace",
            str(workspace_path),
        ],
        console=err_console,
    )
    assert missing_code == 1
    assert "not found" in err_output.getvalue()
