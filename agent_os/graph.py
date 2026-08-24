from collections.abc import Callable

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from agent_os.checkpoints import get_default_checkpointer
from agent_os.nodes import human_gate_node
from agent_os.nodes.architect import architect_node
from agent_os.nodes.executor import executor_node
from agent_os.nodes.planner import planner_node
from agent_os.nodes.supervisor import supervisor_node
from agent_os.nodes.tool_dispatcher import (
    DispatcherCommand,
    build_tool_dispatcher_node,
)
from agent_os.routing import ROUTE_TO_NODE, route_from_state
from agent_os.state import AgentState


def build_graph(
    architect_node_impl: Callable[[AgentState], dict] | None = None,
    executor_node_impl: Callable[[AgentState], dict] | None = None,
    tool_dispatcher_node_impl: (
        Callable[[AgentState], DispatcherCommand] | None
    ) = None,
    checkpointer: BaseCheckpointSaver | None = None,
) -> CompiledStateGraph:
    """Build the graph with an injected or SQLite checkpointer."""
    builder = StateGraph(AgentState)

    if architect_node_impl is None:
        architect_node_impl = architect_node

    if executor_node_impl is None:
        executor_node_impl = executor_node

    if tool_dispatcher_node_impl is None:
        tool_dispatcher_node_impl = build_tool_dispatcher_node()

    if checkpointer is None:
        checkpointer = get_default_checkpointer()

    builder.add_node("planner", planner_node)
    builder.add_node("supervisor", supervisor_node)
    builder.add_node("architect", architect_node_impl)
    builder.add_node("human_gate", human_gate_node)
    builder.add_node("executor", executor_node_impl)
    builder.add_node("tool_dispatcher", tool_dispatcher_node_impl)

    builder.add_edge(START, "planner")
    builder.add_edge("planner", "supervisor")

    builder.add_conditional_edges("supervisor", route_from_state, ROUTE_TO_NODE)

    builder.add_edge("architect", "human_gate")
    builder.add_edge("human_gate", "supervisor")

    builder.add_edge("executor", "supervisor")

    return builder.compile(checkpointer=checkpointer)


graph = build_graph()
