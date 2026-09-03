import sqlite3
from contextlib import closing

import pytest
from langgraph.checkpoint.sqlite import SqliteSaver

from agent_os.checkpoints import (
    CHECKPOINT_DB_ENV,
    get_checkpoint_serializer,
    get_default_checkpointer,
)
from agent_os.schemas import (
    ActionProposal,
    ArchitectBrief,
    CodingPlan,
    CodingResult,
    ExecutionResult,
    PlanArtifact,
)
from agent_os.strategies import StrategyHint


def test_get_default_checkpointer_uses_configured_path(tmp_path, monkeypatch):
    database_path = tmp_path / "nested" / "workflow.db"
    monkeypatch.setenv(CHECKPOINT_DB_ENV, str(database_path))

    checkpointer = get_default_checkpointer()

    assert isinstance(checkpointer, SqliteSaver)
    assert database_path.exists()
    assert checkpointer.conn.execute("PRAGMA journal_mode").fetchone() is not None
    checkpointer.conn.close()


def test_get_default_checkpointer_accepts_explicit_path(tmp_path, monkeypatch):
    environment_path = tmp_path / "ignored.db"
    explicit_path = tmp_path / "explicit.db"
    monkeypatch.setenv(CHECKPOINT_DB_ENV, str(environment_path))

    checkpointer = get_default_checkpointer(explicit_path)

    assert explicit_path.exists()
    assert not environment_path.exists()
    checkpointer.conn.close()


def test_get_default_checkpointer_rejects_blank_environment(monkeypatch):
    monkeypatch.setenv(CHECKPOINT_DB_ENV, "   ")

    with pytest.raises(ValueError, match=f"{CHECKPOINT_DB_ENV} must not be empty"):
        get_default_checkpointer()


def test_closed_checkpointer_releases_database(tmp_path):
    database_path = tmp_path / "reusable.db"
    first = get_default_checkpointer(database_path)
    first.conn.close()

    with closing(sqlite3.connect(database_path)) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)


def test_checkpoint_serializer_round_trips_allowed_application_models():
    serializer = get_checkpoint_serializer()
    brief = ArchitectBrief(
        files=["demo.py"],
        changes=["add logging"],
        verify_cmd="pytest",
    )

    restored = serializer.loads_typed(serializer.dumps_typed(brief))

    assert restored == brief
    assert isinstance(restored, ArchitectBrief)


def test_checkpoint_serializer_round_trips_generic_and_coding_models():
    serializer = get_checkpoint_serializer()

    state_to_serialize = {
        "plan": PlanArtifact(
            summary="test plan",
            proposed_actions=[ActionProposal(tool="ls", reason="check files")],
        ),
        "executor_output": ExecutionResult(status="completed"),
        "coding_plan": CodingPlan(
            summary="coding task",
            files=["main.py"],
            changes=["fix bug"],
            verify_cmd="pytest",
        ),
        "coding_result": CodingResult(status="failed", diff="-bug\n+fix"),
    }

    restored = serializer.loads_typed(serializer.dumps_typed(state_to_serialize))

    assert restored["plan"] == state_to_serialize["plan"]
    assert isinstance(restored["plan"], PlanArtifact)

    assert restored["executor_output"] == state_to_serialize["executor_output"]
    assert isinstance(restored["executor_output"], ExecutionResult)

    assert restored["coding_plan"] == state_to_serialize["coding_plan"]
    assert isinstance(restored["coding_plan"], CodingPlan)

    assert restored["coding_result"] == state_to_serialize["coding_result"]
    assert isinstance(restored["coding_result"], CodingResult)


def test_checkpoint_serializer_round_trips_strategy_hint():
    serializer = get_checkpoint_serializer()
    hint = StrategyHint(
        strategy_id="verification-first-v1",
        version=1,
        task_kind="workflow",
        selection_reason="exploration",
        directive="Inspect constraints before planning.",
    )

    restored = serializer.loads_typed(serializer.dumps_typed({"strategy_hint": hint}))

    assert restored["strategy_hint"] == hint
    assert isinstance(restored["strategy_hint"], StrategyHint)
