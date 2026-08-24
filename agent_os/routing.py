import os
from typing import Literal

from langgraph.graph import END

from agent_os.schemas import (
    ArchitectBrief,
    ExecutionResult,
    ExecutorReport,
    PlanArtifact,
)
from agent_os.state import AgentState

Route = Literal["architect", "executor", "tool", "end"]
RouterMode = Literal["cascade", "direct-escalation"]

ROUTE_TO_NODE: dict[str, str] = {
    "architect": "architect",
    "executor": "executor",
    "tool": "tool_dispatcher",
    "end": END,
}

# Six executed nodes plus LangGraph's final termination superstep.
DEFAULT_RECURSION_LIMIT = 7
DEFAULT_RUNTIME_CONFIG = {"recursion_limit": DEFAULT_RECURSION_LIMIT}


def get_router_mode() -> RouterMode:
    """Return the configured dispatcher mode, preserving cascade by default."""
    value = os.getenv("ROUTER_MODE", "cascade").strip().lower()
    if value not in ("cascade", "direct-escalation"):
        raise ValueError(
            "Invalid ROUTER_MODE value "
            f"'{value}'. Expected 'cascade' or 'direct-escalation'."
        )
    return value


def build_runtime_config(thread_id: str) -> dict[str, object]:
    normalized_thread_id = thread_id.strip()
    if not normalized_thread_id:
        raise ValueError("thread_id must not be empty")
    return {
        "recursion_limit": DEFAULT_RECURSION_LIMIT,
        "configurable": {"thread_id": normalized_thread_id},
    }


def route_from_state(state: AgentState) -> Route:
    executor_output = state.get("executor_output")
    human_feedback = state.get("human_feedback")

    # a. successful ExecutorReport or generic ExecutionResult -> end
    if isinstance(executor_output, ExecutionResult):
        if (
            getattr(executor_output, "status", None) == "completed"
            or getattr(executor_output, "success", None) is True
        ):
            return "end"
    # Legacy check for ExecutorReport before it becomes a subclass
    elif (
        isinstance(executor_output, ExecutorReport) and executor_output.success is True
    ):
        return "end"

    # b. human_feedback == "approved" -> executor
    if human_feedback == "approved":
        return "executor"

    # c. human_feedback beginning "rejected:" -> architect
    if human_feedback and human_feedback.startswith("rejected:"):
        return "architect"

    # legacy string executor_output -> end
    if isinstance(executor_output, str):
        return "end"

    # PlanArtifact without a decision -> end
    if isinstance(state.get("plan"), (PlanArtifact, ArchitectBrief)):
        return "end"

    # When router_escalated is True, supervisor must route to architect
    if state.get("router_escalated") is True:
        return "architect"

    # For a new unresolved task -> tool_dispatcher first
    return "tool"
