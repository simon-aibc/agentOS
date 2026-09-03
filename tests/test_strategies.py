from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from agent_os import strategies
from agent_os.observations import SqliteObservationStore
from agent_os.strategies import StrategyDefinition, select_strategy


def _labelled(
    store: SqliteObservationStore,
    *,
    workspace: str,
    strategy: str,
    signal: str,
    count: int = 1,
) -> None:
    for index in range(count):
        observation = store.create(
            workspace_id=workspace,
            run_id=f"{workspace}-{strategy}-{index}",
            thread_id="thread",
            task_kind="workflow",
            approach=strategy,
            outcome_signal="unknown",
            outcome_evidence="terminal_status=completed",
            source="server.run",
        )
        store.record_outcome(
            observation.observation_id, signal=signal, evidence="operator label"
        )


def test_aggregation_excludes_unknown_and_evidence_backed_selection_is_isolated(
    tmp_path: Path,
) -> None:
    store = SqliteObservationStore(str(tmp_path / "observations.db"))
    _labelled(
        store, workspace="alpha", strategy="default-v1", signal="accepted", count=5
    )
    _labelled(
        store,
        workspace="alpha",
        strategy="verification-first-v1",
        signal="rejected",
        count=5,
    )
    unknown = store.create(
        workspace_id="alpha",
        run_id="unknown",
        thread_id="thread",
        task_kind="workflow",
        approach="default-v1",
        outcome_signal="unknown",
        outcome_evidence="terminal_status=completed",
        source="server.run",
    )

    selected = select_strategy(
        store, workspace_id="alpha", task_kind="workflow", run_id="next"
    )
    assert selected.hint.strategy_id == "default-v1"
    assert selected.hint.selection_reason == "evidence_backed"
    default_pattern = next(
        item for item in selected.patterns if item.strategy_id == "default-v1"
    )
    assert default_pattern.labelled_count == 5
    assert default_pattern.unknown_count == 1
    assert default_pattern.outcome_score == 1.0

    other_workspace = select_strategy(
        store, workspace_id="beta", task_kind="workflow", run_id="first-beta"
    )
    assert other_workspace.hint.selection_reason == "exploration"
    assert store.get(unknown.observation_id).outcome_signal == "unknown"


def test_exploration_balances_atomically_and_repeat_run_is_stable(
    tmp_path: Path,
) -> None:
    database = str(tmp_path / "observations.db")
    SqliteObservationStore(database)

    def choose(index: int) -> str:
        store = SqliteObservationStore(database)
        return select_strategy(
            store,
            workspace_id="workspace",
            task_kind="workflow",
            run_id=f"run-{index}",
        ).hint.strategy_id

    with ThreadPoolExecutor(max_workers=6) as pool:
        selected = list(pool.map(choose, range(12)))
    counts = {strategy: selected.count(strategy) for strategy in set(selected)}
    assert max(counts.values()) - min(counts.values()) <= 1

    store = SqliteObservationStore(database)
    first = select_strategy(
        store, workspace_id="workspace", task_kind="workflow", run_id="run-0"
    )
    assert first.hint.strategy_id == selected[0]


def test_exploration_persists_selected_strategy_version(
    tmp_path: Path, monkeypatch
) -> None:
    definition = StrategyDefinition(
        strategy_id="versioned-exploration-v2",
        version=2,
        task_kind="workflow",
        directive="Use the versioned exploration strategy.",
    )
    monkeypatch.setitem(strategies._REGISTRY, "workflow", (definition,))
    store = SqliteObservationStore(str(tmp_path / "observations.db"))

    selection = select_strategy(
        store,
        workspace_id="workspace",
        task_kind="workflow",
        run_id="versioned-exploration-run",
    )

    assignment = store.get_strategy_assignment("versioned-exploration-run")
    assert assignment is not None
    assert (
        assignment.strategy_id == selection.hint.strategy_id == definition.strategy_id
    )
    assert assignment.strategy_version == selection.hint.version == 2


def test_explicit_override_and_safe_fallbacks(tmp_path: Path) -> None:
    store = SqliteObservationStore(str(tmp_path / "observations.db"))
    explicit = select_strategy(
        store,
        workspace_id="workspace",
        task_kind="workflow",
        run_id="explicit-run",
        explicit_strategy_id="concise-plan-v1",
    )
    assert explicit.hint.strategy_id == "concise-plan-v1"
    assert explicit.hint.selection_reason == "explicit"
    repeated = select_strategy(
        store,
        workspace_id="workspace",
        task_kind="workflow",
        run_id="explicit-run",
    )
    assert repeated.hint.strategy_id == "concise-plan-v1"

    with pytest.raises(ValueError, match="not allowed"):
        select_strategy(
            store,
            workspace_id="workspace",
            task_kind="workflow",
            run_id="bad",
            explicit_strategy_id="untrusted-v1",
        )
    assert (
        select_strategy(
            None, workspace_id="workspace", task_kind="workflow", run_id="none"
        ).hint.strategy_id
        == "default-v1"
    )
    assert (
        select_strategy(
            store, workspace_id="workspace", task_kind="memory_write", run_id="tool"
        ).hint.selection_reason
        == "default"
    )


def test_replay_preserves_evidence_backed_reason_and_sanitized_provenance(
    tmp_path: Path,
) -> None:
    store = SqliteObservationStore(str(tmp_path / "observations.db"))
    _labelled(
        store,
        workspace="alpha",
        strategy="verification-first-v1",
        signal="accepted",
        count=6,
    )
    _labelled(
        store, workspace="alpha", strategy="default-v1", signal="rejected", count=7
    )

    # Initial selection: evidence_backed
    first = select_strategy(
        store, workspace_id="alpha", task_kind="workflow", run_id="run-evidence"
    )
    assert first.hint.strategy_id == "verification-first-v1"
    assert first.hint.selection_reason == "evidence_backed"

    # Replay: must preserve evidence_backed (NOT default)
    replay = select_strategy(
        store, workspace_id="alpha", task_kind="workflow", run_id="run-evidence"
    )
    assert replay.hint.strategy_id == "verification-first-v1"
    assert replay.hint.selection_reason == "evidence_backed"

    # Check persisted StrategyAssignment record
    assignment = store.get_strategy_assignment("run-evidence")
    assert assignment is not None
    assert assignment.strategy_id == "verification-first-v1"
    assert assignment.strategy_version == 1
    assert assignment.selection_reason == "evidence_backed"
    assert assignment.selector_version == "v1.0"

    summary = assignment.evidence_summary
    assert summary is not None
    assert set(summary.keys()) == {
        "labelled_counts",
        "scores",
        "winner",
        "threshold_met",
        "margin",
    }
    assert summary["winner"] == "verification-first-v1"
    assert summary["threshold_met"] is True
    assert summary["labelled_counts"] == {
        "verification-first-v1": 6,
        "default-v1": 7,
        "concise-plan-v1": 0,
        "clarify-first-v1": 0,
    }
    assert summary["scores"]["verification-first-v1"] == 1.0
    assert summary["scores"]["default-v1"] == 0.0


def test_replay_preserves_explicit_override_reason(tmp_path: Path) -> None:
    store = SqliteObservationStore(str(tmp_path / "observations.db"))
    first = select_strategy(
        store,
        workspace_id="alpha",
        task_kind="workflow",
        run_id="run-explicit",
        explicit_strategy_id="concise-plan-v1",
    )
    assert first.hint.strategy_id == "concise-plan-v1"
    assert first.hint.selection_reason == "explicit"

    replay = select_strategy(
        store,
        workspace_id="alpha",
        task_kind="workflow",
        run_id="run-explicit",
    )
    assert replay.hint.strategy_id == "concise-plan-v1"
    assert replay.hint.selection_reason == "explicit"

    assignment = store.get_strategy_assignment("run-explicit")
    assert assignment is not None
    assert assignment.selection_reason == "explicit"
    assert assignment.evidence_summary is None


def test_replay_preserves_exploration_reason(tmp_path: Path) -> None:
    store = SqliteObservationStore(str(tmp_path / "observations.db"))
    first = select_strategy(
        store,
        workspace_id="alpha",
        task_kind="workflow",
        run_id="run-exploration",
    )
    assert first.hint.selection_reason == "exploration"

    replay = select_strategy(
        store,
        workspace_id="alpha",
        task_kind="workflow",
        run_id="run-exploration",
    )
    assert replay.hint.strategy_id == first.hint.strategy_id
    assert replay.hint.selection_reason == "exploration"

    assignment = store.get_strategy_assignment("run-exploration")
    assert assignment is not None
    assert assignment.selection_reason == "exploration"
    assert assignment.evidence_summary is None


def test_audit_snapshot_contains_no_raw_text(tmp_path: Path) -> None:
    store = SqliteObservationStore(str(tmp_path / "observations.db"))
    # Create observations with raw task text, feedback, evidence
    obs = store.create(
        workspace_id="alpha",
        run_id="raw-run-1",
        thread_id="thread-secret",
        task_kind="workflow",
        approach="default-v1",
        outcome_signal="unknown",
        outcome_evidence="secret task instructions and private tokens",
        source="server.run",
    )
    store.record_outcome(
        obs.observation_id,
        signal="rejected",
        evidence="private feedback on security credentials",
    )

    for idx in range(5):
        obs2 = store.create(
            workspace_id="alpha",
            run_id=f"raw-run-v-{idx}",
            thread_id="thread-secret",
            task_kind="workflow",
            approach="verification-first-v1",
            outcome_signal="unknown",
            outcome_evidence="super private task content",
            source="server.run",
        )
        store.record_outcome(
            obs2.observation_id, signal="accepted", evidence="great result"
        )

    for idx in range(4):
        obs3 = store.create(
            workspace_id="alpha",
            run_id=f"raw-run-d-{idx}",
            thread_id="thread-secret",
            task_kind="workflow",
            approach="default-v1",
            outcome_signal="unknown",
            outcome_evidence="other confidential data",
            source="server.run",
        )
        store.record_outcome(
            obs3.observation_id, signal="rejected", evidence="bad result"
        )

    select_strategy(
        store, workspace_id="alpha", task_kind="workflow", run_id="audit-run"
    )
    assignment = store.get_strategy_assignment("audit-run")
    assert assignment is not None
    assert assignment.selection_reason == "evidence_backed"
    summary = assignment.evidence_summary
    assert summary is not None

    import json

    summary_json = json.dumps(summary)
    # Check no raw text strings leaked
    for forbidden in [
        "secret",
        "private",
        "confidential",
        "credentials",
        "feedback",
        "instructions",
    ]:
        assert forbidden not in summary_json.lower()


def test_clarify_first_strategy_registered_and_selectable(tmp_path: Path) -> None:
    workflow_strategies = strategies.strategies_for("workflow")
    strategy_ids = [s.strategy_id for s in workflow_strategies]
    assert "clarify-first-v1" in strategy_ids

    store = SqliteObservationStore(str(tmp_path / "observations.db"))
    explicit = select_strategy(
        store,
        workspace_id="workspace",
        task_kind="workflow",
        run_id="clarify-run",
        explicit_strategy_id="clarify-first-v1",
    )
    assert explicit.hint.strategy_id == "clarify-first-v1"
    assert explicit.hint.selection_reason == "explicit"
    assert "binary" in explicit.hint.directive
    assert "acceptance criteria" in explicit.hint.directive
    assert "reversible" in explicit.hint.directive

    assignment = store.get_strategy_assignment("clarify-run")
    assert assignment is not None
    assert assignment.strategy_id == "clarify-first-v1"
    assert assignment.selection_reason == "explicit"
    assert assignment.strategy_version == 1
