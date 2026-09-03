import pytest

from agent_os.routing import (
    DEFAULT_RUNTIME_CONFIG,
    build_runtime_config,
    route_from_state,
)
from agent_os.schemas import ArchitectBrief, CodingPlan, CodingResult, ExecutorReport


def test_default_runtime_config_allows_workflow_termination():
    """The runtime config allows six nodes plus the termination superstep."""
    assert DEFAULT_RUNTIME_CONFIG == {"recursion_limit": 7}


def test_build_runtime_config_normalizes_and_requires_thread_id():
    assert build_runtime_config("  workflow-1  ") == {
        "recursion_limit": 7,
        "configurable": {"thread_id": "workflow-1"},
    }
    for invalid_thread_id in ("", " ", "\t\n"):
        with pytest.raises(ValueError, match="thread_id must not be empty"):
            build_runtime_config(invalid_thread_id)


def test_successful_executor_report_routes_to_end():
    state = {
        "messages": [],
        "task": "A normal task",
        "plan": None,
        "executor_output": ExecutorReport(
            diff="done", verify_output="ok", success=True
        ),
        "hot_context": None,
    }
    assert route_from_state(state) == "end"


def test_successful_coding_result_routes_to_end():
    state = {
        "messages": [],
        "task": "A normal task",
        "plan": None,
        "executor_output": CodingResult(
            status="completed", diff="done", verify_output="ok"
        ),
        "hot_context": None,
    }
    assert route_from_state(state) == "end"


def test_failed_executor_report_with_approved_feedback_retries_executor():
    state = {
        "messages": [],
        "task": "A normal task",
        "plan": None,
        "executor_output": ExecutorReport(
            diff="", verify_output="failed", success=False
        ),
        "human_feedback": "approved",
        "hot_context": None,
    }
    assert route_from_state(state) == "executor"


def test_route_unresolved_task_to_tool():
    """For a new unresolved task -> tool_dispatcher first."""
    state = {
        "messages": [],
        "task": "research the codebase",
        "plan": None,
        "executor_output": None,
        "human_feedback": None,
        "hot_context": None,
    }
    assert route_from_state(state) == "tool"


def test_route_escalated_to_architect():
    """When router_escalated is True, supervisor must route to architect."""
    state = {
        "messages": [],
        "task": "A normal task",
        "plan": None,
        "executor_output": None,
        "human_feedback": None,
        "hot_context": None,
        "router_escalated": True,
    }
    assert route_from_state(state) == "architect"


def test_route_executor_output():
    """Legacy string executor output still terminates the workflow."""
    state = {
        "messages": [],
        "task": "A normal task",
        "plan": None,
        "executor_output": "Task completed successfully",
        "human_feedback": None,
        "hot_context": None,
    }
    assert route_from_state(state) == "end"


def test_route_default_fallback_to_tool():
    """A new unresolved task reaches the dispatcher."""
    state = {
        "messages": [],
        "task": "A normal task",
        "plan": None,
        "executor_output": None,
        "human_feedback": None,
        "hot_context": None,
    }
    assert route_from_state(state) == "tool"


def test_route_approved_feedback_to_executor():
    state = {
        "messages": [],
        "task": "do something",
        "plan": None,
        "executor_output": None,
        "human_feedback": "approved",
        "hot_context": None,
    }
    assert route_from_state(state) == "executor"


def test_route_rejected_feedback_to_architect():
    state = {
        "messages": [],
        "task": "do something",
        "plan": None,
        "executor_output": None,
        "human_feedback": "rejected: bad plan",
        "hot_context": None,
    }
    assert route_from_state(state) == "architect"


def test_architect_brief_without_feedback_routes_to_end():
    state = {
        "messages": [],
        "task": "do something",
        "plan": ArchitectBrief(
            files=["demo.py"],
            changes=["add logging"],
            verify_cmd="pytest",
        ),
        "executor_output": None,
        "human_feedback": None,
        "hot_context": None,
    }

    assert route_from_state(state) == "end"


def test_coding_plan_without_feedback_routes_to_end():
    state = {
        "messages": [],
        "task": "do something",
        "plan": CodingPlan(
            summary="test",
            files=["demo.py"],
            changes=["add logging"],
            verify_cmd="pytest",
        ),
        "executor_output": None,
        "human_feedback": None,
        "hot_context": None,
    }

    assert route_from_state(state) == "end"
