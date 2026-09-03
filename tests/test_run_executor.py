from types import SimpleNamespace
from typing import Any

import pytest
from langgraph.types import Command

from agent_os.checkpoints import CHECKPOINT_DB_ENV
from agent_os.runs import create_run, get_run, list_events
from agent_os.schemas import ExecutorReport, ToolExecutionResult
from agent_os.server import run_executor
from agent_os.server.run_executor import execute_run


class FakeGraph:
    def __init__(
        self,
        events: list[dict[str, Any]],
        snapshot: object,
        *,
        stream_error: Exception | None = None,
    ) -> None:
        self.events = events
        self.snapshot = snapshot
        self.stream_error = stream_error
        self.seen_input: object | None = None
        self.seen_config: dict[str, Any] | None = None
        self.seen_version: str | None = None

    async def astream_events(
        self,
        graph_input: object,
        config: dict[str, Any],
        version: str,
    ):
        self.seen_input = graph_input
        self.seen_config = config
        self.seen_version = version
        if self.stream_error is not None:
            raise self.stream_error
        for event in self.events:
            yield event

    async def aget_state(self, config: dict[str, Any]) -> object:
        self.seen_config = config
        return self.snapshot


@pytest.fixture
def runs_db(tmp_path, monkeypatch):
    database_path = tmp_path / "checkpoints.db"
    monkeypatch.setenv(CHECKPOINT_DB_ENV, str(database_path))
    monkeypatch.setenv("AGENT_OS_SANDBOX", str(tmp_path))
    monkeypatch.setenv("AGENT_OS_PERMISSIONS_DB", str(tmp_path / "permissions.db"))
    monkeypatch.setenv("AGENT_OS_OBSERVATIONS_DB", str(tmp_path / "observations.db"))
    return database_path


def _completed_snapshot(values: dict[str, Any] | None = None) -> object:
    return SimpleNamespace(tasks=(), values=values or {})


def _interrupted_snapshot(prompt: object = "Approve?") -> object:
    interrupt = SimpleNamespace(value=prompt)
    task = SimpleNamespace(interrupts=(interrupt,))
    return SimpleNamespace(tasks=(task,))


def _patch_graph(monkeypatch, graph: FakeGraph) -> None:
    def build_graph(checkpointer=None):
        return graph

    monkeypatch.setattr("agent_os.graph.build_graph", build_graph)


def _observations_for_store(store_path, workspace) -> list[object]:
    from agent_os.observations import SqliteObservationStore, observation_workspace_id

    return SqliteObservationStore(str(store_path)).list(
        workspace_id=observation_workspace_id(workspace)
    )


@pytest.mark.anyio
async def test_execute_run_translates_node_token_and_result_events(
    runs_db, monkeypatch
):
    graph = FakeGraph(
        [
            {"event": "on_chain_start", "name": "planner"},
            {
                "event": "on_chat_model_stream",
                "data": {"chunk": SimpleNamespace(content="Hel")},
            },
            {
                "event": "on_chat_model_stream",
                "data": {"chunk": SimpleNamespace(content="lo")},
            },
            {"event": "on_chain_end", "name": "planner"},
        ],
        _completed_snapshot(),
    )
    _patch_graph(monkeypatch, graph)
    workspace = runs_db.parent / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("AGENT_OS_SANDBOX", str(runs_db.parent))
    run_id = create_run("thread-1", str(workspace), "task")

    await execute_run(run_id, "thread-1", "task")

    assert graph.seen_config == {
        "recursion_limit": 7,
        "configurable": {"thread_id": "thread-1"},
    }
    assert graph.seen_version == "v2"
    assert get_run(run_id)["status"] == "completed"
    events = list_events(run_id)
    assert [event["kind"] for event in events] == [
        "node",
        "token",
        "token",
        "node",
        "result",
    ]
    assert events[0]["payload"] == {"name": "planner", "event": "on_chain_start"}
    assert events[1]["payload"] == {"content": "Hel"}
    observations = _observations_for_store(runs_db.parent / "observations.db", None)
    assert len(observations) == 1
    assert observations[0].outcome_signal == "unknown"
    assert observations[0].outcome_evidence == "terminal_status=completed"


@pytest.mark.anyio
async def test_execute_run_includes_workspace_skill_output_in_result_event(
    runs_db, monkeypatch
):
    graph = FakeGraph(
        [],
        _completed_snapshot(
            {
                "tool_result": ToolExecutionResult(
                    tool="hermes_chat",
                    output='{"content":"hello"}',
                    success=True,
                )
            }
        ),
    )
    _patch_graph(monkeypatch, graph)
    run_id = create_run("thread-skill", None, "hermes-chat hello")

    await execute_run(run_id, "thread-skill", "hermes-chat hello")

    result_event = list_events(run_id)[-1]
    assert result_event["kind"] == "result"
    assert result_event["payload"] == {
        "tool_result": {
            "tool": "hermes_chat",
            "output": '{"content":"hello"}',
            "success": True,
        }
    }


@pytest.mark.anyio
async def test_execute_run_surfaces_executor_self_check_in_result_event(
    runs_db, monkeypatch
):
    graph = FakeGraph(
        [],
        _completed_snapshot(
            {
                "executor_output": ExecutorReport(
                    status="completed",
                    self_check={
                        "total": 2,
                        "met": 1,
                        "results": [
                            {
                                "rule_id": "one",
                                "passed": True,
                                "method": "deterministic",
                                "evidence": "ok",
                            }
                        ],
                    },
                )
            }
        ),
    )
    _patch_graph(monkeypatch, graph)
    run_id = create_run("thread-self-check", None, "validate")

    await execute_run(run_id, "thread-self-check", "validate")

    assert list_events(run_id)[-1]["payload"] == {
        "self_check": {
            "total": 2,
            "met": 1,
            "results": [
                {
                    "rule_id": "one",
                    "passed": True,
                    "method": "deterministic",
                    "evidence": "ok",
                }
            ],
        }
    }


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("tool_status", "expected_run_status"),
    (("cancelled", "cancelled"), ("failed", "error")),
)
async def test_execute_run_preserves_unsuccessful_native_tool_outcome(
    runs_db, monkeypatch, tool_status, expected_run_status
):
    graph = FakeGraph(
        [],
        _completed_snapshot(
            {
                "tool_result": ToolExecutionResult(
                    tool="memory_write",
                    output=(
                        '{"status": "' + tool_status + '", '
                        '"errors": ["Policy did not commit the write"]}'
                    ),
                    success=False,
                )
            }
        ),
    )
    _patch_graph(monkeypatch, graph)
    run_id = create_run("thread-unsuccessful-tool", None, "memory_write note.md :: x")

    await execute_run(run_id, "thread-unsuccessful-tool", "memory_write note.md :: x")

    run = get_run(run_id)
    assert run["status"] == expected_run_status
    assert run["error"] == "Policy did not commit the write"
    assert run["ended_at"] is not None


@pytest.mark.anyio
async def test_execute_run_binds_the_persisted_workspace(runs_db, monkeypatch):
    from agent_os.sandbox import get_sandbox_root

    workspace = runs_db.parent / "project"
    workspace.mkdir()
    monkeypatch.setenv("AGENT_OS_SANDBOX", str(runs_db.parent))

    class WorkspaceGraph(FakeGraph):
        async def astream_events(self, graph_input, config, version):
            assert get_sandbox_root() == workspace.resolve()
            if False:
                yield {}

    graph = WorkspaceGraph([], _completed_snapshot())
    _patch_graph(monkeypatch, graph)
    run_id = create_run("thread-workspace", str(workspace), "task")

    await execute_run(run_id, "thread-workspace", "task")

    assert get_run(run_id)["status"] == "completed"


@pytest.mark.anyio
async def test_execute_run_records_interrupt_and_resume_command(runs_db, monkeypatch):
    graph = FakeGraph([], _interrupted_snapshot("Review plan"))
    _patch_graph(monkeypatch, graph)
    run_id = create_run("thread-2", None, "task")

    await execute_run(run_id, "thread-2", "task", resume_feedback="approved")

    assert isinstance(graph.seen_input, Command)
    assert get_run(run_id)["status"] == "interrupted"
    events = list_events(run_id)
    assert [event["kind"] for event in events] == ["interrupt"]
    assert events[0]["payload"] == {"prompt": "Review plan"}
    assert not (runs_db.parent / "observations.db").exists()


@pytest.mark.anyio
async def test_execute_run_records_error_path(runs_db, monkeypatch):
    graph = FakeGraph([], _completed_snapshot(), stream_error=RuntimeError("boom"))
    _patch_graph(monkeypatch, graph)
    run_id = create_run("thread-3", None, "task")

    await execute_run(run_id, "thread-3", "task")

    run = get_run(run_id)
    assert run["status"] == "error"
    assert run["error"] == "boom"
    assert run["ended_at"] is not None
    events = list_events(run_id)
    assert [event["kind"] for event in events] == ["error"]
    assert events[0]["payload"] == {"message": "boom"}


@pytest.mark.anyio
async def test_terminal_observation_never_leaks_task_or_tool_output(
    runs_db, monkeypatch
):
    secret = "TOP-SECRET-task-and-output"
    graph = FakeGraph(
        [],
        _completed_snapshot(
            {
                "tool_result": ToolExecutionResult(
                    tool="memory_write",
                    output=secret,
                    success=True,
                )
            }
        ),
    )
    _patch_graph(monkeypatch, graph)
    workspace = runs_db.parent / "private-workspace"
    workspace.mkdir()
    run_id = create_run("thread-private", str(workspace), f"task {secret}")

    await execute_run(run_id, "thread-private", f"task {secret}")

    observation = _observations_for_store(runs_db.parent / "observations.db", None)[0]
    serialized = str(observation.to_dict())
    assert secret not in serialized
    assert observation.task_kind == "memory_write"
    assert observation.task_signature is not None
    assert observation.approach == "native_tool:memory_write"


@pytest.mark.anyio
async def test_observation_failure_does_not_change_terminal_run_status(
    runs_db, monkeypatch
):
    graph = FakeGraph([], _completed_snapshot())
    _patch_graph(monkeypatch, graph)
    from agent_os import server
    from agent_os.policy import LocalPolicy

    monkeypatch.setattr(
        server.runtime,
        "composed_workspace",
        lambda: (_ for _ in ()).throw(RuntimeError("workspace unavailable")),
    )
    monkeypatch.setattr(
        run_executor,
        "runtime_config",
        lambda thread_id: {"configurable": {"thread_id": thread_id}},
    )
    monkeypatch.setattr(
        run_executor,
        "runtime_policy_for_session",
        lambda session_key: LocalPolicy(session_key=session_key),
    )
    monkeypatch.setattr(
        run_executor, "build_runtime_graph", lambda *, checkpointer: graph
    )
    monkeypatch.setattr(
        run_executor,
        "initial_state",
        lambda task, **_kwargs: {"task": task},
    )
    run_id = create_run("thread-no-observation", None, "task")

    await execute_run(run_id, "thread-no-observation", "task")

    assert get_run(run_id)["status"] == "completed"


@pytest.mark.anyio
async def test_terminal_observation_uses_selected_strategy(runs_db, monkeypatch):
    workspace = runs_db.parent / "strategy-workspace"
    workspace.mkdir()
    graph = FakeGraph(
        [],
        _completed_snapshot(
            {
                "strategy_hint": {
                    "strategy_id": "verification-first-v1",
                    "task_kind": "workflow",
                }
            }
        ),
    )
    _patch_graph(monkeypatch, graph)
    run_id = create_run("thread-strategy", str(workspace), "task")

    await execute_run(run_id, "thread-strategy", "task")

    observation = _observations_for_store(runs_db.parent / "observations.db", None)[0]
    assert observation.task_kind == "workflow"
    assert observation.approach == "verification-first-v1"


@pytest.mark.anyio
async def test_terminal_observation_uses_composed_runtime_workspace(
    runs_db, monkeypatch
):
    from agent_os import server
    from agent_os.observations import observation_workspace_id
    from agent_os.policy import LocalPolicy

    monkeypatch.delenv("AGENT_OS_OBSERVATIONS_DB", raising=False)
    execution_workspace = runs_db.parent / "execution-workspace"
    runtime_workspace = runs_db.parent / "runtime-workspace"
    execution_workspace.mkdir()
    runtime_workspace.mkdir()
    graph = FakeGraph([], _completed_snapshot())
    _patch_graph(monkeypatch, graph)
    monkeypatch.setattr(
        server.runtime,
        "composed_workspace",
        lambda: SimpleNamespace(workspace=runtime_workspace),
    )
    monkeypatch.setattr(
        run_executor,
        "runtime_config",
        lambda thread_id: {"configurable": {"thread_id": thread_id}},
    )
    monkeypatch.setattr(
        run_executor,
        "runtime_policy_for_session",
        lambda session_key: LocalPolicy(session_key=session_key),
    )
    monkeypatch.setattr(
        run_executor, "build_runtime_graph", lambda *, checkpointer: graph
    )
    monkeypatch.setattr(
        run_executor,
        "initial_state",
        lambda task, **_kwargs: {"task": task},
    )
    run_id = create_run("thread-runtime-workspace", str(execution_workspace), "task")

    await execute_run(run_id, "thread-runtime-workspace", "task")

    observations = _observations_for_store(
        runtime_workspace / "observations.db", runtime_workspace
    )
    assert get_run(run_id)["status"] == "completed"
    assert len(observations) == 1
    assert observations[0].workspace_id == observation_workspace_id(runtime_workspace)
    assert observations[0].workspace_id != observation_workspace_id(execution_workspace)
    assert not (execution_workspace / "observations.db").exists()
