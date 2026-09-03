"""Pure, evidence-bounded validation shared by execution and skill authoring.

This module deliberately does not execute commands, inspect the filesystem, or
import workflow code. Callers provide the execution evidence it may inspect.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from types import MappingProxyType
from typing import Literal

from pydantic import BaseModel, ConfigDict


class ValidationRule(BaseModel):
    """A single validation rule, safe to persist or replay."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    description: str
    kind: Literal["deterministic", "llm"]
    check: str | None = None
    param: str | None = None


class ValidationResult(BaseModel):
    """The auditable outcome of one validation rule."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    rule_id: str
    passed: bool
    method: Literal["deterministic", "llm"]
    evidence: str


LlmJudge = Callable[[ValidationRule, Mapping[str, object]], bool | tuple[bool, str]]


def _text(value: object) -> str:
    if isinstance(value, str):
        return value
    return ""


def _artifacts(context: Mapping[str, object]) -> tuple[str, ...]:
    raw_artifacts = context.get("artifacts", ())
    if isinstance(raw_artifacts, str):
        return (raw_artifacts,)
    if not isinstance(raw_artifacts, Sequence):
        return ()
    return tuple(item for item in raw_artifacts if isinstance(item, str))


def _actual_output(context: Mapping[str, object]) -> str:
    """Return only independent execution evidence, never a plan self-claim."""
    parts = [_text(context.get("verify_output")), _text(context.get("diff"))]
    parts.extend(_artifacts(context))
    return "\n".join(part for part in parts if part)


def _verify_command(
    rule: ValidationRule, context: Mapping[str, object]
) -> tuple[bool, str]:
    if not rule.param:
        return False, "verify_cmd requires the command key in param."

    commands = context.get("verify_commands", context.get("verify_cmds", {}))
    if not isinstance(commands, Mapping) or rule.param not in commands:
        return False, f"No supplied verification result for: {rule.param}"

    outcome = commands[rule.param]
    if isinstance(outcome, bool):
        return outcome, f"{rule.param}: supplied boolean result={outcome}."
    if isinstance(outcome, int):
        return outcome == 0, f"{rule.param}: exit_code={outcome}."
    if not isinstance(outcome, Mapping):
        return False, f"{rule.param}: supplied result has no exit status."

    exit_code = outcome.get("exit_code", outcome.get("returncode"))
    output = _text(outcome.get("output")) or _text(outcome.get("stdout"))
    if not isinstance(exit_code, int):
        return False, f"{rule.param}: supplied result has no integer exit status."
    evidence = f"{rule.param}: exit_code={exit_code}"
    if output:
        evidence = f"{evidence}; output={output}"
    return exit_code == 0, evidence


def _file_exists(
    rule: ValidationRule, context: Mapping[str, object]
) -> tuple[bool, str]:
    if not rule.param:
        return False, "file_exists requires the expected path in param."

    artifacts = _artifacts(context)
    if rule.param in artifacts:
        return True, f"Found {rule.param} in supplied artifacts."
    if rule.param in _text(context.get("diff")):
        return True, f"Found {rule.param} in supplied diff."
    return False, f"{rule.param} is absent from supplied artifacts and diff."


def _regex(rule: ValidationRule, context: Mapping[str, object]) -> tuple[bool, str]:
    if not rule.param:
        return False, "regex requires the pattern in param."
    try:
        matched = re.search(rule.param, _actual_output(context)) is not None
    except re.error as error:
        return False, f"Invalid regex {rule.param!r}: {error}."
    return (
        matched,
        f"Regex {rule.param!r} {'matched' if matched else 'did not match'} supplied output.",
    )


def _contains(rule: ValidationRule, context: Mapping[str, object]) -> tuple[bool, str]:
    if not rule.param:
        return False, "contains requires the expected text in param."
    matched = rule.param in _actual_output(context)
    return (
        matched,
        f"Text {rule.param!r} {'was found in' if matched else 'was absent from'} supplied output.",
    )


_DETERMINISTIC_CHECKERS: Mapping[
    str, Callable[[ValidationRule, Mapping[str, object]], tuple[bool, str]]
] = {
    "verify_cmd": _verify_command,
    "file_exists": _file_exists,
    "regex": _regex,
    "contains": _contains,
}


def _run_llm_judge(
    rule: ValidationRule,
    context: Mapping[str, object],
    llm_judge: LlmJudge | None,
) -> ValidationResult:
    if llm_judge is None:
        return ValidationResult(
            rule_id=rule.id,
            passed=False,
            method="llm",
            evidence="No LLM judge was injected for this semantic rule.",
        )

    evidence_context = MappingProxyType(
        {
            key: context[key]
            for key in ("verify_output", "diff", "artifacts")
            if key in context
        }
    )
    try:
        judged = llm_judge(rule, evidence_context)
    except Exception as error:  # The validation layer must fail closed.
        return ValidationResult(
            rule_id=rule.id,
            passed=False,
            method="llm",
            evidence=f"LLM judge failed: {type(error).__name__}.",
        )

    if isinstance(judged, tuple):
        passed, evidence = judged
        return ValidationResult(
            rule_id=rule.id,
            passed=bool(passed),
            method="llm",
            evidence=str(evidence),
        )
    return ValidationResult(
        rule_id=rule.id,
        passed=bool(judged),
        method="llm",
        evidence="LLM judge evaluated the supplied independent evidence.",
    )


def run_validation(
    rules: Sequence[ValidationRule],
    context: Mapping[str, object],
    *,
    llm_judge: LlmJudge | None = None,
) -> list[ValidationResult]:
    """Run deterministic checks or an explicitly injected LLM judge.

    ``verify_commands`` is caller-provided evidence keyed by command text. A
    value can be an exit integer, boolean, or mapping with ``exit_code`` (or
    ``returncode``) and optional ``output``/``stdout``. No command is run here.
    """
    results: list[ValidationResult] = []
    for rule in rules:
        if rule.kind == "llm":
            results.append(_run_llm_judge(rule, context, llm_judge))
            continue

        checker = _DETERMINISTIC_CHECKERS.get(rule.check or "")
        if checker is None:
            results.append(
                ValidationResult(
                    rule_id=rule.id,
                    passed=False,
                    method="deterministic",
                    evidence=f"Unknown deterministic checker: {rule.check!r}.",
                )
            )
            continue
        passed, evidence = checker(rule, context)
        results.append(
            ValidationResult(
                rule_id=rule.id,
                passed=passed,
                method="deterministic",
                evidence=evidence,
            )
        )
    return results


def summarize(results: Sequence[ValidationResult]) -> dict[str, object]:
    """Return a JSON-safe M/N summary while preserving each audit record."""
    materialized = list(results)
    return {
        "total": len(materialized),
        "met": sum(result.passed for result in materialized),
        "results": [result.model_dump() for result in materialized],
    }
