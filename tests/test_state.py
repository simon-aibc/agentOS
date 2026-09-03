import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from pydantic import TypeAdapter, ValidationError

from agent_os.graph import build_graph
from agent_os.routing import build_runtime_config
from agent_os.schemas import (
    ArchitectBrief,
    BashResult,
    EditFileResult,
    ExecutorReport,
    RouterDecision,
    ToolExecutionResult,
)
from agent_os.state import AgentState


def test_agent_state_keys():
    """AgentState exposes six required and five optional persisted keys."""
    # TypedDict.__annotations__ gives the keys and types
    expected_required_keys = {
        "messages",
        "task",
        "plan",
        "executor_output",
        "human_feedback",
        "hot_context",
    }
    expected_all_keys = expected_required_keys | {
        "tool_result",
        "router_escalated",
        "backend_binding",
        "conversation_summary",
        "observation_context",
            "strategy_hint",
            "run_id",
        }
    assert set(AgentState.__annotations__.keys()) == expected_all_keys
    # TypedDict fields are required unless declared with NotRequired.
    # Note: TypedDict.__required_keys__ excludes fields declared as NotRequired.
    assert set(AgentState.__required_keys__) == expected_required_keys
    assert "tool_result" not in AgentState.__required_keys__
    assert "router_escalated" not in AgentState.__required_keys__
    assert "backend_binding" not in AgentState.__required_keys__


def test_agent_state_validation_success():
    """2. Pydantic TypeAdapter(AgentState) accepts a complete valid state."""
    adapter = TypeAdapter(AgentState)
    valid_state = {
        "messages": [],
        "task": "Do something",
        "plan": "My plan",
        "executor_output": "Success",
        "human_feedback": "approved",
        "hot_context": "Some context",
    }
    # Should not raise exception
    validated = adapter.validate_python(valid_state)
    assert validated["task"] == "Do something"


def test_agent_state_validation_failure():
    """3. Validation rejects a state missing a required key."""
    adapter = TypeAdapter(AgentState)
    invalid_state = {
        "messages": [],
        "task": "Do something",
        "plan": "My plan",
        # Missing other keys
    }
    with pytest.raises(ValidationError):
        adapter.validate_python(invalid_state)


@pytest.mark.parametrize("confidence", [-0.01, 1.01])
def test_router_decision_rejects_out_of_bounds_confidence(confidence):
    with pytest.raises(ValidationError):
        RouterDecision(tool=None, confidence=confidence)


def test_agent_state_accepts_optional_r7_dispatch_fields():
    adapter = TypeAdapter(AgentState)
    state = {
        "messages": [],
        "task": "read README.md",
        "plan": None,
        "executor_output": None,
        "human_feedback": None,
        "hot_context": None,
        "tool_result": ToolExecutionResult(
            tool="read_file",
            output="contents",
            success=True,
        ),
        "router_escalated": False,
    }

    validated = adapter.validate_python(state)

    assert validated["tool_result"].tool == "read_file"
    assert validated["router_escalated"] is False


def fake_architect_node(state):
    return {
        "plan": ArchitectBrief(files=["dummy"], changes=["dummy"], verify_cmd="dummy")
    }


def test_build_graph_compiles():
    """4. build_graph() compiles without error."""
    graph = build_graph(
        architect_node_impl=fake_architect_node,
        checkpointer=InMemorySaver(),
    )
    assert graph is not None

    g = graph.get_graph()
    expected_nodes = {
        "__start__",
        "planner",
        "supervisor",
        "architect",
        "human_gate",
        "executor",
        "tool_dispatcher",
        "__end__",
    }
    assert set(g.nodes.keys()) == expected_nodes

    edges = {(e.source, e.target) for e in g.edges}
    expected_edges = {
        ("__start__", "planner"),
        ("planner", "supervisor"),
        ("supervisor", "architect"),
        ("supervisor", "executor"),
        ("supervisor", "tool_dispatcher"),
        ("supervisor", "__end__"),
        ("architect", "human_gate"),
        ("human_gate", "supervisor"),
        ("executor", "supervisor"),
        ("tool_dispatcher", "supervisor"),
        ("tool_dispatcher", "__end__"),
    }
    assert edges == expected_edges


def test_graph_invocation():
    """5. Invocation reaches the architect and pauses at the human gate."""
    graph = build_graph(
        architect_node_impl=fake_architect_node,
        tool_dispatcher_node_impl=lambda state: Command(
            update={"router_escalated": True},
            goto="supervisor",
        ),
        checkpointer=InMemorySaver(),
    )
    initial_state = {
        "messages": [],
        "task": "Test task",
        "plan": None,
        "executor_output": None,
        "human_feedback": None,
        "hot_context": None,
    }

    config = build_runtime_config("thread-test-1")

    # Run the graph
    result = graph.invoke(initial_state, config=config)

    # Assert values
    assert result["task"] == "Test task"

    assert isinstance(result["plan"], ArchitectBrief)
    assert result["plan"].files == ["dummy"]

    assert result["executor_output"] is None
    assert result["human_feedback"] is None
    assert result["hot_context"] is None
    assert result["messages"] == []


def test_architect_brief_validation():
    """ArchitectBrief validates a correct payload."""
    brief = ArchitectBrief(files=["a.py"], changes=["fix"], verify_cmd="pytest")
    assert brief.files == ["a.py"]
    assert brief.changes == ["fix"]
    assert brief.verify_cmd == "pytest"


def test_architect_brief_missing_fields():
    """Missing fields fail validation."""
    with pytest.raises(ValidationError):
        ArchitectBrief(files=["a.py"])


def test_agent_state_plan_accepts_brief():
    """AgentState accepts both a string plan and ArchitectBrief."""
    adapter = TypeAdapter(AgentState)
    valid_state = {
        "messages": [],
        "task": "Do something",
        "plan": ArchitectBrief(files=["file1"], changes=["fix"], verify_cmd="ls"),
        "executor_output": None,
        "human_feedback": None,
        "hot_context": None,
    }
    validated = adapter.validate_python(valid_state)
    assert validated["plan"].files == ["file1"]


def test_executor_report_validation():
    report = ExecutorReport(diff="foo", verify_output="ok", success=True)
    assert report.success is True


def test_edit_file_result_validation():
    res = EditFileResult(path="foo", bytes_written=10)
    assert res.bytes_written == 10


def test_bash_result_validation():
    res = BashResult(
        args=["ls"], returncode=0, stdout="out", stderr="", timed_out=False
    )
    assert res.args == ["ls"]


def test_agent_state_executor_output_accepts_report():
    adapter = TypeAdapter(AgentState)
    valid_state = {
        "messages": [],
        "task": "Do something",
        "plan": None,
        "executor_output": ExecutorReport(diff="foo", verify_output="ok", success=True),
        "human_feedback": None,
        "hot_context": None,
    }
    validated = adapter.validate_python(valid_state)
    assert validated["executor_output"].success is True

    # Legacy string should also work
    valid_state["executor_output"] = "Legacy output string"
    validated2 = adapter.validate_python(valid_state)
    assert validated2["executor_output"] == "Legacy output string"
