from unittest import mock

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.errors import GraphRecursionError
from langgraph.types import Command

from agent_os.graph import build_graph
from agent_os.routing import build_runtime_config
from agent_os.schemas import ExecutorReport
from agent_os.state import AgentState


def test_recursion_limit_enforced():
    """A rejected architect loop stops at the configured runtime bound."""

    def looping_architect_node(state: AgentState) -> dict:
        return {}

    with mock.patch(
        "agent_os.graph.human_gate_node",
        side_effect=lambda state: {"human_feedback": "rejected: auto"},
    ):
        graph = build_graph(
            architect_node_impl=looping_architect_node,
            tool_dispatcher_node_impl=lambda state: Command(
                update={"router_escalated": True},
                goto="supervisor",
            ),
            checkpointer=InMemorySaver(),
        )

    state: AgentState = {
        "messages": [],
        "task": "infinite loop task",
        "plan": None,
        "executor_output": None,
        "human_feedback": None,
        "hot_context": None,
    }

    with pytest.raises(GraphRecursionError):
        graph.invoke(state, config=build_runtime_config("architect-retry-loop"))


def test_recursion_limit_executor_retry():
    """A failing approved executor loop also stops at the runtime bound."""

    def failing_executor_node(state: AgentState) -> dict[str, ExecutorReport]:
        return {
            "executor_output": ExecutorReport(
                diff="partial change",
                verify_output="tests failed",
                success=False,
            )
        }

    graph = build_graph(
        executor_node_impl=failing_executor_node,
        checkpointer=InMemorySaver(),
    )
    state: AgentState = {
        "messages": [],
        "task": "infinite loop executor task",
        "plan": None,
        "executor_output": None,
        "human_feedback": "approved",
        "hot_context": None,
    }

    with pytest.raises(GraphRecursionError):
        graph.invoke(state, config=build_runtime_config("executor-retry-loop"))
