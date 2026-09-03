from unittest.mock import MagicMock, patch

import pytest

from agent_os.cli_backends import CliBackendError
from agent_os.nodes.executor import _attach_self_check, executor_node
from agent_os.schemas import ArchitectBrief, ExecutionResult, ExecutorReport


@pytest.fixture(autouse=True)
def clear_cli_executor_backend(monkeypatch):
    monkeypatch.delenv("LLM_EXECUTOR", raising=False)


@patch("agent_os.nodes.executor.build_executor_agent")
def test_executor_node_logic(mock_build_agent):
    """Test executor_node wrapper logic directly."""
    mock_agent = MagicMock()
    mock_build_agent.return_value = mock_agent

    report = ExecutorReport(diff="foo", verify_output="bar", success=True)
    # Simulate a successful structured response from LangGraph
    mock_agent.invoke.return_value = {"structured_response": report}

    brief = ArchitectBrief(files=["f1"], changes=["c1"], verify_cmd="v1")

    state = {
        "messages": [],
        "task": "do executor",
        "plan": brief,
        "executor_output": None,
        "hot_context": None,
    }

    result = executor_node(state)

    mock_build_agent.assert_called_once()
    mock_agent.invoke.assert_called_once()
    assert result == {"executor_output": report}


def test_executor_node_invalid_plan():
    """Test executor_node raises ValueError if plan is not an ArchitectBrief."""
    state = {
        "messages": [],
        "task": "do executor",
        "plan": "just a string",
        "executor_output": None,
        "hot_context": None,
    }

    with pytest.raises(
        ValueError, match="Executor requires a PlanArtifact or ArchitectBrief plan."
    ):
        executor_node(state)


def test_executor_node_cli_backend_routing(monkeypatch):
    """Test executor_node routes to CLI when LLM_EXECUTOR starts with cli/"""
    monkeypatch.setenv("LLM_EXECUTOR", "cli/codex")

    plan = ArchitectBrief(files=["f1"], changes=["c1"], verify_cmd="v1")
    report = ExecutorReport(diff="d", verify_output="vo", success=True)
    state = {
        "messages": [],
        "task": "do executor",
        "plan": plan,
        "executor_output": None,
        "hot_context": None,
    }

    with patch(
        "agent_os.nodes.executor.build_cli_executor_invoker"
    ) as mock_build_invoker:
        mock_invoker = MagicMock(return_value=report)
        mock_build_invoker.return_value = mock_invoker

        result = executor_node(state)

        mock_build_invoker.assert_called_once_with("codex")
        mock_invoker.assert_called_once()
        passed_state = mock_invoker.call_args[0][0]
        # State passed to CLI invoker is copied and has trimmed messages
        assert passed_state is not state
        assert len(passed_state["messages"]) == 1
        assert (
            "Please execute this ArchitectBrief" in passed_state["messages"][0].content
        )
        assert state["messages"] == []
        assert result == {"executor_output": report}


def test_executor_node_cli_unknown_backend(monkeypatch):
    monkeypatch.setenv("LLM_EXECUTOR", "cli/unknown")
    plan = ArchitectBrief(files=["f1"], changes=["c1"], verify_cmd="v1")
    state = {
        "messages": [],
        "task": "do",
        "plan": plan,
    }

    with pytest.raises(ValueError, match="Unsupported CLI executor backend"):
        executor_node(state)


def test_executor_node_cli_partial_change_warning(monkeypatch):
    monkeypatch.setenv("LLM_EXECUTOR", "cli/claude-code")
    plan = ArchitectBrief(files=["f1"], changes=["c1"], verify_cmd="v1")
    state = {
        "messages": [],
        "task": "do",
        "plan": plan,
    }

    def mock_invoker(state):
        raise CliBackendError("Subprocess failed")

    with patch(
        "agent_os.nodes.executor.build_cli_executor_invoker"
    ) as mock_build_invoker:
        mock_build_invoker.return_value = mock_invoker

        with pytest.raises(
            RuntimeError,
            match=r"CLI executor failed \(CliBackendError\)\. Partial sandbox changes",
        ):
            executor_node(state)


def test_executor_node_cli_invalid_output_warns_about_partial_changes(monkeypatch):
    monkeypatch.setenv("LLM_EXECUTOR", "cli/codex")
    plan = ArchitectBrief(files=["f1"], changes=["c1"], verify_cmd="v1")
    state = {"messages": [], "task": "do", "plan": plan}
    mock_invoker = MagicMock(side_effect=ValueError("invalid structured output"))

    with patch(
        "agent_os.nodes.executor.build_cli_executor_invoker",
        return_value=mock_invoker,
    ):
        with pytest.raises(RuntimeError, match="Partial sandbox changes"):
            executor_node(state)

    mock_invoker.assert_called_once()


def test_executor_node_cli_quota_fallback(monkeypatch):
    """Test that when codex hits a usage limit error, it falls back to claude-code."""
    monkeypatch.setenv("LLM_EXECUTOR", "cli/codex")
    plan = ArchitectBrief(files=["f1"], changes=["c1"], verify_cmd="v1")
    report = ExecutorReport(diff="d", verify_output="vo", success=True)
    state = {"messages": [], "task": "do", "plan": plan}

    mock_codex_invoker = MagicMock(
        side_effect=CliBackendError(
            "Command 'codex' failed with return code 1. Stderr excerpt: ERROR: You've hit your usage limit. Upgrade to Pro"
        )
    )
    mock_claude_invoker = MagicMock(return_value=report)

    def mock_build_invoker(backend):
        if backend == "codex":
            return mock_codex_invoker
        if backend == "claude-code":
            return mock_claude_invoker
        raise ValueError(f"Unknown backend: {backend}")

    with patch(
        "agent_os.nodes.executor.build_cli_executor_invoker",
        side_effect=mock_build_invoker,
    ):
        result = executor_node(state)

    mock_codex_invoker.assert_called_once()
    mock_claude_invoker.assert_called_once()
    assert result == {"executor_output": report}


def test_execution_result_self_check_defaults_and_serializes() -> None:
    result = ExecutionResult(status="completed")

    assert result.self_check == {}
    assert result.model_dump()["self_check"] == {}


@patch("agent_os.nodes.executor.build_executor_agent")
def test_executor_node_react_attaches_deterministic_self_check(mock_build_agent):
    report = ExecutorReport(
        diff="+++ b/report.txt",
        verify_output="all checks passed",
        artifacts=["report.txt"],
        status="completed",
    )
    mock_build_agent.return_value.invoke.return_value = {"structured_response": report}
    plan = ArchitectBrief(
        files=["report.txt"],
        changes=["write report"],
        verify_cmd="pytest -q",
        acceptance_criteria=["contains: all checks passed", "Tệp `report.txt` tồn tại"],
    )

    completed = executor_node({"messages": [], "task": "do", "plan": plan})[
        "executor_output"
    ]

    assert completed.self_check["total"] == completed.self_check["met"] == 2
    assert [item["method"] for item in completed.self_check["results"]] == [
        "deterministic",
        "deterministic",
    ]
    assert report.self_check == {}


def test_attach_self_check_recognizes_vietnamese_file_criterion_with_location() -> None:
    plan = ArchitectBrief(
        files=["artifacts/report.txt"],
        changes=["write report"],
        verify_cmd="pytest -q",
        acceptance_criteria=["Tệp `artifacts/report.txt` tồn tại tại thư mục gốc."],
    )

    completed = _attach_self_check(
        ExecutorReport(status="completed", artifacts=["artifacts/report.txt"]), plan
    )

    assert completed.self_check["total"] == completed.self_check["met"] == 1
    assert completed.self_check["results"][0]["method"] == "deterministic"


def test_attach_self_check_reports_unmet_criteria_with_audit_evidence() -> None:
    plan = ArchitectBrief(
        files=["missing.txt"],
        changes=["write file"],
        verify_cmd="pytest -q",
        acceptance_criteria=["file missing.txt exists", "contains: completed"],
    )
    report = ExecutorReport(
        status="completed", verify_output="not completed", artifacts=[]
    )

    completed = _attach_self_check(report, plan)

    assert completed.self_check["total"] == 2
    assert completed.self_check["met"] == 1
    assert completed.self_check["results"][0]["passed"] is False
    assert completed.self_check["results"][0]["method"] == "deterministic"
    assert "absent" in completed.self_check["results"][0]["evidence"]


def test_verify_cmd_requires_explicit_reported_exit_code() -> None:
    plan = ArchitectBrief(
        files=["f1"],
        changes=["c1"],
        verify_cmd="pytest -q",
        acceptance_criteria=["verify_cmd: pytest -q"],
    )

    passed = _attach_self_check(
        ExecutorReport(
            status="completed", verify_output="pytest: exit_code=0; 12 passed"
        ),
        plan,
    )
    unproven = _attach_self_check(
        ExecutorReport(status="completed", verify_output="12 passed"),
        plan,
    )

    assert passed.self_check["met"] == 1
    assert unproven.self_check["met"] == 0
    assert (
        "No supplied verification result"
        in unproven.self_check["results"][0]["evidence"]
    )


def test_executor_node_cli_success_attaches_self_check(monkeypatch):
    monkeypatch.setenv("LLM_EXECUTOR", "cli/codex")
    plan = ArchitectBrief(
        files=["f1"],
        changes=["c1"],
        verify_cmd="v1",
        acceptance_criteria=["contains: cli verification passed"],
    )
    report = ExecutorReport(status="completed", verify_output="cli verification passed")

    with patch(
        "agent_os.nodes.executor.build_cli_executor_invoker",
        return_value=MagicMock(return_value=report),
    ):
        completed = executor_node({"messages": [], "task": "do", "plan": plan})[
            "executor_output"
        ]

    assert completed.self_check["total"] == completed.self_check["met"] == 1


def test_executor_node_cli_fallback_attaches_self_check(monkeypatch):
    monkeypatch.setenv("LLM_EXECUTOR", "cli/codex")
    plan = ArchitectBrief(
        files=["f1"],
        changes=["c1"],
        verify_cmd="v1",
        acceptance_criteria=["contains: fallback verification passed"],
    )
    fallback_report = ExecutorReport(
        status="completed", verify_output="fallback verification passed"
    )

    def build_invoker(backend):
        if backend == "codex":
            return MagicMock(side_effect=CliBackendError("usage limit"))
        assert backend == "claude-code"
        return MagicMock(return_value=fallback_report)

    with patch(
        "agent_os.nodes.executor.build_cli_executor_invoker", side_effect=build_invoker
    ):
        completed = executor_node({"messages": [], "task": "do", "plan": plan})[
            "executor_output"
        ]

    assert completed.self_check["total"] == completed.self_check["met"] == 1
