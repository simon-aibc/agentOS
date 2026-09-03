import ast
from pathlib import Path

import pytest
from pydantic import ValidationError

from agent_os.validation import (
    ValidationResult,
    ValidationRule,
    run_validation,
    summarize,
)


def _rule(check: str, param: str) -> ValidationRule:
    return ValidationRule(
        id=check,
        description=f"Check {check}",
        kind="deterministic",
        check=check,
        param=param,
    )


@pytest.mark.parametrize(
    ("context", "expected_passed"),
    [
        (
            {"verify_commands": {"pytest -q": {"exit_code": 0, "output": "12 passed"}}},
            True,
        ),
        (
            {"verify_commands": {"pytest -q": {"returncode": 1, "stdout": "1 failed"}}},
            False,
        ),
    ],
)
def test_verify_cmd_uses_only_supplied_command_result(
    context: dict[str, object], expected_passed: bool
) -> None:
    result = run_validation([_rule("verify_cmd", "pytest -q")], context)[0]

    assert result.passed is expected_passed
    assert result.method == "deterministic"


@pytest.mark.parametrize(
    ("context", "expected_passed"),
    [
        ({"artifacts": ["agent_os/validation.py"]}, True),
        ({"diff": "+++ b/agent_os/validation.py\n"}, True),
        ({"artifacts": ["tests/test_validation.py"], "diff": ""}, False),
    ],
)
def test_file_exists_checks_only_artifacts_or_diff(
    context: dict[str, object], expected_passed: bool
) -> None:
    result = run_validation([_rule("file_exists", "agent_os/validation.py")], context)[
        0
    ]

    assert result.passed is expected_passed
    assert result.method == "deterministic"


@pytest.mark.parametrize(
    ("check", "param", "context", "expected_passed"),
    [
        ("regex", r"\b12 passed\b", {"verify_output": "pytest: 12 passed"}, True),
        ("regex", r"\b12 passed\b", {"verify_output": "pytest: 1 failed"}, False),
        ("contains", "validation complete", {"diff": "validation complete"}, True),
        ("contains", "validation complete", {"artifacts": ["validation.py"]}, False),
    ],
)
def test_output_checkers_pass_and_fail(
    check: str,
    param: str,
    context: dict[str, object],
    expected_passed: bool,
) -> None:
    result = run_validation([_rule(check, param)], context)[0]

    assert result.passed is expected_passed
    assert result.method == "deterministic"


def test_malformed_regex_and_unknown_checker_fail_closed() -> None:
    malformed = run_validation([_rule("regex", "[")], {"verify_output": "anything"})[0]
    unknown = run_validation([_rule("not_a_checker", "anything")], {})[0]

    assert malformed.passed is False
    assert "Invalid regex" in malformed.evidence
    assert unknown.passed is False
    assert "Unknown deterministic checker" in unknown.evidence


def test_summarize_counts_met_rules_and_preserves_audit_records() -> None:
    summary = summarize(
        [
            ValidationResult(
                rule_id="one", passed=True, method="deterministic", evidence="ok"
            ),
            ValidationResult(
                rule_id="two", passed=False, method="llm", evidence="not met"
            ),
        ]
    )

    assert summary == {
        "total": 2,
        "met": 1,
        "results": [
            {
                "rule_id": "one",
                "passed": True,
                "method": "deterministic",
                "evidence": "ok",
            },
            {"rule_id": "two", "passed": False, "method": "llm", "evidence": "not met"},
        ],
    }


def test_llm_judge_is_optional_injected_and_receives_only_independent_evidence() -> (
    None
):
    rule = ValidationRule(
        id="semantic",
        description="Semantic criterion",
        kind="llm",
    )
    context = {
        "verify_output": "independent test evidence",
        "diff": "actual diff",
        "artifacts": ["artifact.txt"],
        "plan_summary": "self-claim that must not reach the judge",
    }

    missing_judge = run_validation([rule], context)[0]
    assert missing_judge.passed is False
    assert missing_judge.method == "llm"

    received: dict[str, object] = {}

    def judge(received_rule: ValidationRule, evidence: object) -> tuple[bool, str]:
        assert received_rule == rule
        received.update(dict(evidence))
        return True, "independent evidence supports the criterion"

    result = run_validation([rule], context, llm_judge=judge)[0]
    assert result == ValidationResult(
        rule_id="semantic",
        passed=True,
        method="llm",
        evidence="independent evidence supports the criterion",
    )
    assert received == {
        "verify_output": "independent test evidence",
        "diff": "actual diff",
        "artifacts": ["artifact.txt"],
    }


def test_rule_is_frozen_and_forbids_unknown_fields() -> None:
    rule = _rule("contains", "evidence")

    with pytest.raises(ValidationError):
        ValidationRule(
            id="bad",
            description="bad",
            kind="deterministic",
            check="contains",
            param="evidence",
            unexpected=True,
        )
    with pytest.raises(ValidationError):
        rule.id = "changed"  # type: ignore[misc]


def test_validation_module_stays_standalone() -> None:
    source = Path(__file__).parents[1] / "agent_os" / "validation.py"
    imports = {
        alias.name.split(".")[0]
        for node in ast.walk(ast.parse(source.read_text()))
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imports.update(
        node.module.split(".")[0]
        for node in ast.walk(ast.parse(source.read_text()))
        if isinstance(node, ast.ImportFrom) and node.module
    )

    assert {"graph", "executor", "skills"}.isdisjoint(imports)
