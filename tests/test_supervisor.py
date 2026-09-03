from langgraph.graph import END

from agent_os.nodes.supervisor import supervisor_node
from agent_os.routing import ROUTE_TO_NODE, route_from_state


def test_supervisor_node_routes_to_tool():
    """Supervisor should return a Command with goto='tool_dispatcher' for tool routes."""
    state = {
        "messages": [],
        "task": "Please search",
        "plan": None,
        "executor_output": None,
        "hot_context": None,
    }
    logical_route = route_from_state(state)
    assert logical_route == "tool"

    command = supervisor_node(state)
    assert command.goto == ROUTE_TO_NODE[logical_route]
    assert command.goto == "tool_dispatcher"


def test_supervisor_node_routes_to_end():
    """Supervisor should return a Command with goto=END for end routes."""
    state = {
        "messages": [],
        "task": "Normal task",
        "plan": None,
        "executor_output": "Done",
        "hot_context": None,
    }
    logical_route = route_from_state(state)
    assert logical_route == "end"

    command = supervisor_node(state)
    assert command.goto == ROUTE_TO_NODE[logical_route]
    assert command.goto == END


def test_supervisor_node_routes_to_executor():
    """Supervisor should return a Command with goto='executor' for executor routes."""
    state = {
        "messages": [],
        "task": "Normal task",
        "plan": None,
        "executor_output": None,
        "human_feedback": "approved",
        "hot_context": None,
    }
    logical_route = route_from_state(state)
    assert logical_route == "executor"

    command = supervisor_node(state)
    assert command.goto == ROUTE_TO_NODE[logical_route]
    assert command.goto == "executor"


def test_supervisor_node_routes_to_architect():
    """Supervisor should return a Command with goto='architect' for architect routes."""
    state = {
        "messages": [],
        "task": "Normal task",
        "plan": None,
        "executor_output": None,
        "hot_context": None,
        "router_escalated": True,
    }
    logical_route = route_from_state(state)
    assert logical_route == "architect"

    command = supervisor_node(state)
    assert command.goto == ROUTE_TO_NODE[logical_route]
    assert command.goto == "architect"
