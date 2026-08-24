from agent_os.schemas import CodingPlan, CodingResult, ExecutionResult, PlanArtifact
from agent_os.state import AgentState


def test_agent_state_accepts_all_plan_types():
    # TypedDict construction
    state_none: AgentState = {
        "messages": [],
        "task": "test",
        "plan": None,
        "executor_output": None,
        "human_feedback": None,
        "hot_context": None,
    }

    state_str: AgentState = {
        "messages": [],
        "task": "test",
        "plan": "string plan",
        "executor_output": "string output",
        "human_feedback": None,
        "hot_context": None,
    }

    state_generic: AgentState = {
        "messages": [],
        "task": "test",
        "plan": PlanArtifact(summary="summary"),
        "executor_output": ExecutionResult(status="completed"),
        "human_feedback": None,
        "hot_context": None,
    }

    state_coding: AgentState = {
        "messages": [],
        "task": "test",
        "plan": CodingPlan(summary="coding", files=["a.py"], changes=[], verify_cmd=""),
        "executor_output": CodingResult(status="completed", diff="diff"),
        "human_feedback": None,
        "hot_context": None,
    }

    # Assert that they are constructed correctly
    assert state_none["plan"] is None
    assert state_str["plan"] == "string plan"
    assert isinstance(state_generic["plan"], PlanArtifact)
    assert isinstance(state_coding["plan"], CodingPlan)
    assert state_none["executor_output"] is None
    assert state_str["executor_output"] == "string output"
    assert isinstance(state_generic["executor_output"], ExecutionResult)
    assert isinstance(state_coding["executor_output"], CodingResult)
