import time
from unittest.mock import MagicMock

from langchain_core.messages import AIMessage

from agent_os.nodes.executor import _attach_self_check
from agent_os.schemas import CodingResult, ExecutorReport, PlanArtifact
from agent_os.semantic_judge import (
    _format_evidence_context,
    _parse_judge_response,
    build_llm_judge,
)
from agent_os.validation import ValidationRule


def test_parse_judge_response():
    passed, reason = _parse_judge_response("PASS\nCriterion met properly.")
    assert passed is True
    assert reason == "Criterion met properly."

    passed, reason = _parse_judge_response("FAIL\nOutput lacks required schema.")
    assert passed is False
    assert reason == "Output lacks required schema."

    passed, reason = _parse_judge_response("PASS")
    assert passed is True
    assert reason == "PASS"

    passed, reason = _parse_judge_response("invalid response")
    assert passed is False
    assert "Indeterminate" in reason


def test_format_evidence_context():
    context = {
        "verify_output": "exit_code=0",
        "diff": "+ new line",
        "artifacts": ["artifact.json"],
    }
    formatted = _format_evidence_context(context)
    assert "=== Verify Command Output ===" in formatted
    assert "exit_code=0" in formatted
    assert "=== Code / File Diff ===" in formatted
    assert "+ new line" in formatted
    assert "=== Generated Artifacts ===" in formatted
    assert "artifact.json" in formatted


def test_build_llm_judge_pass():
    mock_llm = MagicMock()
    mock_llm.invoke.return_value = AIMessage(
        content="PASS\nVerified changes in diff match requirements."
    )

    judge = build_llm_judge(mock_llm)
    rule = ValidationRule(
        id="c1",
        description="The report must summarize quarterly revenue accurately.",
        kind="llm",
    )
    context = {
        "verify_output": "Report generated",
        "diff": "revenue: 1.2M",
    }
    passed, evidence = judge(rule, context)
    assert passed is True
    assert "Verified changes in diff" in evidence
    assert mock_llm.invoke.call_count == 1


def test_build_llm_judge_fail_closed_on_exception():
    mock_llm = MagicMock()
    mock_llm.invoke.side_effect = RuntimeError("Rate limit or connection drop")

    judge = build_llm_judge(mock_llm)
    rule = ValidationRule(id="c1", description="Semantic check", kind="llm")
    passed, evidence = judge(rule, {})
    assert passed is False
    assert "LLM judge execution error" in evidence


def test_build_llm_judge_timeout():
    mock_llm = MagicMock()

    def _slow_invoke(messages):
        time.sleep(0.3)
        return AIMessage(content="PASS\nLate answer.")

    mock_llm.invoke.side_effect = _slow_invoke

    judge = build_llm_judge(mock_llm, timeout_s=0.05)
    rule = ValidationRule(id="c1", description="Timeout test", kind="llm")
    passed, evidence = judge(rule, {})
    assert passed is False
    assert evidence == "LLM judge timeout"


def test_build_llm_judge_token_cap_bind():
    mock_llm = MagicMock()
    mock_bound = MagicMock()
    mock_bound.invoke.return_value = AIMessage(content="PASS\nBound success.")
    mock_llm.bind.return_value = mock_bound

    judge = build_llm_judge(mock_llm, max_tokens=128)
    mock_llm.bind.assert_called_once_with(max_tokens=128)

    rule = ValidationRule(id="c1", description="Token cap test", kind="llm")
    passed, evidence = judge(rule, {})
    assert passed is True
    assert mock_bound.invoke.call_count == 1


def test_attach_self_check_mixed_deterministic_and_semantic():
    mock_llm = MagicMock()
    mock_llm.invoke.return_value = AIMessage(
        content="PASS\nCode comments are well explained."
    )
    judge = build_llm_judge(mock_llm)

    plan = PlanArtifact(
        goal="Test goal",
        steps=["Step 1"],
        acceptance_criteria=[
            "contains: target string",
            "Code comments are well explained and clear.",
        ],
    )
    report = CodingResult(
        status="completed",
        diff="target string added with clear comments",
    )

    updated_report = _attach_self_check(report, plan, llm_judge=judge)
    self_check = updated_report.self_check

    assert self_check is not None
    assert self_check["total"] == 2
    assert self_check["met"] == 2

    results = self_check["results"]
    assert len(results) == 2

    # Deterministic check
    assert results[0]["rule_id"] == "criterion-1"
    assert results[0]["method"] == "deterministic"
    assert results[0]["passed"] is True

    # Semantic LLM check
    assert results[1]["rule_id"] == "criterion-2"
    assert results[1]["method"] == "llm"
    assert results[1]["passed"] is True
    assert "Code comments are well explained." in results[1]["evidence"]


def test_attach_self_check_without_judge_fails_closed_safely():
    plan = PlanArtifact(
        goal="Test goal",
        steps=["Step 1"],
        acceptance_criteria=[
            "Purely semantic criteria without configured LLM",
        ],
    )
    report = ExecutorReport(
        status="completed",
    )

    updated_report = _attach_self_check(report, plan, llm_judge=None)
    self_check = updated_report.self_check
    assert self_check is not None
    assert self_check["total"] == 1
    assert self_check["met"] == 0
    assert self_check["results"][0]["passed"] is False
    assert "No LLM judge" in self_check["results"][0]["evidence"]
