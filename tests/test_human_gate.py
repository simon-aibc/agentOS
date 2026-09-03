from unittest.mock import patch

import pytest

from agent_os.nodes.human_gate import human_gate_node, normalize_human_feedback
from agent_os.schemas import ArchitectBrief, CodingPlan
from agent_os.state import AgentState


def make_state(plan: str | ArchitectBrief | None) -> AgentState:
    return {
        "messages": [],
        "task": "review this plan",
        "plan": plan,
        "executor_output": None,
        "human_feedback": None,
        "hot_context": None,
    }


def test_normalize_human_feedback_approved():
    assert normalize_human_feedback("approved") == "approved"
    assert normalize_human_feedback("y") == "approved"
    assert normalize_human_feedback("yes") == "approved"
    assert normalize_human_feedback(" YEs ") == "approved"


def test_normalize_human_feedback_rejected():
    assert normalize_human_feedback("rejected") == "rejected: no reason provided"
    assert normalize_human_feedback("n") == "rejected: no reason provided"
    assert normalize_human_feedback("no") == "rejected: no reason provided"

    # Check reason retention (case insensitive for prefix, retains case for reason)
    assert (
        normalize_human_feedback("rejected: I don't like it")
        == "rejected: I don't like it"
    )
    assert normalize_human_feedback("REJECTED:  Bad plan ") == "rejected: Bad plan"
    assert normalize_human_feedback("rejected:") == "rejected: no reason provided"
    assert normalize_human_feedback("REJECTED:   ") == "rejected: no reason provided"


def test_normalize_human_feedback_invalid():
    with pytest.raises(ValueError):
        normalize_human_feedback("")
    with pytest.raises(ValueError):
        normalize_human_feedback("maybe")
    with pytest.raises(ValueError):
        normalize_human_feedback(123)


def test_human_gate_node_non_architect_brief():
    with pytest.raises(ValueError, match="requires a PlanArtifact or ArchitectBrief"):
        human_gate_node(make_state("just a string"))


@patch("agent_os.nodes.human_gate.interrupt")
def test_human_gate_node_interrupts(mock_interrupt):
    mock_interrupt.return_value = "y"

    plan = CodingPlan(summary="test", files=["a.py"], changes=["fix"], verify_cmd="ls")
    result = human_gate_node(make_state(plan))

    mock_interrupt.assert_called_once()
    args = mock_interrupt.call_args.args
    assert "CodingPlan JSON" in args[0] or "ArchitectBrief JSON" in args[0]
    assert '"a.py"' in args[0]
    assert '"verify_cmd": "ls"' in args[0]

    assert result == {"human_feedback": "approved"}


@patch("agent_os.nodes.human_gate.interrupt")
def test_human_gate_node_includes_acceptance_criteria(mock_interrupt):
    mock_interrupt.return_value = "approved"

    plan = CodingPlan(
        summary="Clarify plan",
        files=["file1.py"],
        changes=["modify"],
        verify_cmd="pytest",
        acceptance_criteria=[
            "Criterion 1: binary check",
            "Criterion 2: reversibility classification",
        ],
    )
    result = human_gate_node(make_state(plan))

    mock_interrupt.assert_called_once()
    args = mock_interrupt.call_args.args
    assert '"acceptance_criteria"' in args[0]
    assert '"Criterion 1: binary check"' in args[0]
    assert result == {"human_feedback": "approved"}
