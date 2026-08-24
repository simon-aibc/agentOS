from typing import cast

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from agent_os.graph import build_graph
from agent_os.routing import build_runtime_config
from agent_os.schemas import ArchitectBrief, ExecutorReport
from agent_os.state import AgentState


def make_state(**overrides: object) -> AgentState:
    state: dict[str, object] = {
        "messages": [],
        "task": "do something normal",
        "plan": None,
        "executor_output": None,
        "human_feedback": None,
        "hot_context": None,
    }
    state.update(overrides)
    return cast(AgentState, state)


class ArchitectSpy:
    def __init__(self) -> None:
        self.feedback: list[str | None] = []

    def __call__(self, state: AgentState) -> dict[str, ArchitectBrief]:
        feedback = state.get("human_feedback")
        self.feedback.append(feedback)
        change = "implement requested change"
        if feedback and feedback.startswith("rejected:"):
            change = f"revise plan using feedback: {feedback}"
        return {
            "plan": ArchitectBrief(
                files=["dummy.py"],
                changes=[change],
                verify_cmd="pytest",
            )
        }


class ExecutorSpy:
    def __init__(self) -> None:
        self.call_count = 0

    def __call__(self, state: AgentState) -> dict[str, ExecutorReport]:
        self.call_count += 1
        return {
            "executor_output": ExecutorReport(
                diff="updated dummy.py",
                verify_output="tests passed",
                success=True,
            )
        }


class ToolDispatcherSpy:
    def __init__(self) -> None:
        self.call_count = 0

    def __call__(self, state: AgentState) -> Command:
        self.call_count += 1
        return Command(update={"router_escalated": True}, goto="supervisor")


def build_test_graph():
    architect = ArchitectSpy()
    executor = ExecutorSpy()
    dispatcher = ToolDispatcherSpy()
    checkpointer = InMemorySaver()
    graph = build_graph(
        architect_node_impl=architect,
        executor_node_impl=executor,
        tool_dispatcher_node_impl=dispatcher,
        checkpointer=checkpointer,
    )
    return graph, architect, executor, dispatcher, checkpointer


def test_build_graph_compiles_with_default_and_injected_checkpointers(
    tmp_path,
    monkeypatch,
):
    database_path = tmp_path / "default.db"
    monkeypatch.setenv("AGENT_OS_CHECKPOINTS_DB", str(database_path))
    default_graph = build_graph(
        architect_node_impl=ArchitectSpy(),
        executor_node_impl=ExecutorSpy(),
    )
    assert isinstance(default_graph.checkpointer, SqliteSaver)
    assert database_path.exists()
    default_graph.checkpointer.conn.close()

    graph, architect, executor, dispatcher, checkpointer = build_test_graph()
    assert graph.checkpointer is checkpointer
    assert set(graph.nodes) == {
        "__start__",
        "planner",
        "supervisor",
        "architect",
        "human_gate",
        "executor",
        "tool_dispatcher",
    }


def test_default_checkpointer_requires_thread_id():
    graph = build_graph(
        architect_node_impl=ArchitectSpy(),
        executor_node_impl=ExecutorSpy(),
        checkpointer=InMemorySaver(),
    )

    with pytest.raises(ValueError, match="thread_id"):
        graph.invoke(make_state())


def test_graph_pauses_at_human_gate_before_executor():
    graph, architect, executor, dispatcher, _ = build_test_graph()
    config = build_runtime_config("pause-flow")

    result = graph.invoke(make_state(), config=config)

    assert architect.feedback == [None]
    assert executor.call_count == 0
    assert "__interrupt__" in result
    prompt = result["__interrupt__"][0].value
    assert "ArchitectBrief JSON" in prompt
    assert '"dummy.py"' in prompt
    assert '"verify_cmd": "pytest"' in prompt

    snapshot = graph.get_state(config)
    assert snapshot.next == ("human_gate",)
    assert snapshot.tasks[0].interrupts[0].value == prompt


def test_graph_approve_resume_executes_once_and_terminates():
    graph, architect, executor, dispatcher, _ = build_test_graph()
    config = build_runtime_config("approve-flow")

    paused = graph.invoke(make_state(), config=config)
    assert "__interrupt__" in paused
    assert architect.feedback == [None]
    assert executor.call_count == 0

    result = graph.invoke(Command(resume="approved"), config=config)

    assert result["human_feedback"] == "approved"
    assert isinstance(result["executor_output"], ExecutorReport)
    assert result["executor_output"].success is True
    assert executor.call_count == 1
    assert graph.get_state(config).next == ()

    repeated = graph.invoke(Command(resume="approved"), config=config)
    assert repeated == result
    assert executor.call_count == 1


def test_graph_rejection_returns_feedback_to_architect_and_pauses_again():
    graph, architect, executor, dispatcher, _ = build_test_graph()
    config = build_runtime_config("reject-flow")

    graph.invoke(make_state(), config=config)
    assert architect.feedback == [None]
    assert executor.call_count == 0

    feedback = "rejected: add rollback tests"
    result = graph.invoke(Command(resume=feedback), config=config)

    assert "__interrupt__" in result
    assert architect.feedback == [None, feedback]
    assert executor.call_count == 0
    assert "revise plan using feedback" in result["plan"].changes[0]

    snapshot = graph.get_state(config)
    assert snapshot.next == ("human_gate",)
    assert snapshot.values["human_feedback"] == feedback
    assert snapshot.values["executor_output"] is None


def test_graph_accepts_short_human_feedback_alias():
    graph, _, executor, dispatcher, _ = build_test_graph()
    config = build_runtime_config("feedback-alias")

    graph.invoke(make_state(), config=config)
    result = graph.invoke(Command(resume="y"), config=config)

    assert result["human_feedback"] == "approved"
    assert executor.call_count == 1


def test_graph_thread_checkpoints_are_isolated():
    graph, _, _, dispatcher, _ = build_test_graph()
    first_config = build_runtime_config("thread-one")
    second_config = build_runtime_config("thread-two")

    graph.invoke(make_state(task="first task"), config=first_config)

    assert graph.get_state(first_config).next == ("human_gate",)
    assert graph.get_state(second_config).next == ()
    assert graph.get_state(second_config).values == {}


def test_graph_skill_task_routes_to_tool_dispatcher():
    graph, _, executor, dispatcher, _ = build_test_graph()
    config = build_runtime_config("skill-route")

    visited = [
        next(iter(step))
        for step in graph.stream(
            make_state(task="please research the repository"),
            config=config,
        )
    ]

    assert visited == [
        "planner",
        "supervisor",
        "tool_dispatcher",
        "supervisor",
        "architect",
        "__interrupt__",
    ]
    assert executor.call_count == 0


def test_graph_tier1_native_tool_terminates_without_llm(tmp_path, monkeypatch):
    target = tmp_path / "e2e.txt"
    target.write_text("tier one complete", encoding="utf-8")
    monkeypatch.delenv("AGENT_OS_SANDBOX", raising=False)
    monkeypatch.chdir(tmp_path)
    graph = build_graph(checkpointer=InMemorySaver())
    config = build_runtime_config("tier-one-native-tool")

    visited = [
        next(iter(step))
        for step in graph.stream(
            make_state(task="read e2e.txt"),
            config=config,
        )
    ]
    result = graph.get_state(config).values

    assert visited == ["planner", "supervisor", "tool_dispatcher"]
    assert result["tool_result"].tool == "read_file"
    assert result["tool_result"].success is True
    assert "tier one complete" in result["tool_result"].output
    assert result["router_escalated"] is False


def test_graph_completed_output_terminates_without_executor():
    graph, architect, executor, dispatcher, _ = build_test_graph()
    config = build_runtime_config("completed-output")
    report = ExecutorReport(diff="done", verify_output="passed", success=True)

    visited = [
        next(iter(step))
        for step in graph.stream(
            make_state(executor_output=report),
            config=config,
        )
    ]

    assert visited == ["planner", "supervisor"]
    assert architect.feedback == []
    assert executor.call_count == 0
