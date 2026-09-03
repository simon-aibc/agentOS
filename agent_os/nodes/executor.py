import os
import re

from agent_os.agents.cli_executor import build_cli_executor_invoker
from agent_os.agents.executor import build_executor_agent
from agent_os.cli_backends import CliBackendError
from agent_os.llm import (
    get_architect_llm,
    get_executor_llm,
    get_router_llm,
    invoke_with_llm_retry,
)
from agent_os.messages import trim_agent_messages
from agent_os.schemas import (
    ArchitectBrief,
    ExecutionResult,
    ExecutorReport,
    PlanArtifact,
)
from agent_os.semantic_judge import build_llm_judge
from agent_os.state import AgentState
from agent_os.validation import LlmJudge, ValidationRule, run_validation, summarize

_EXPLICIT_CHECK = re.compile(
    r"^(file_exists|regex|contains|verify_cmd)\s*:\s*(.+)$", re.IGNORECASE
)
_FILE_EXISTS = re.compile(
    r"(?:file|tệp)\s+[`\"']?([\w./-]+)[`\"']?\s+(?:exists|is present|tồn tại)(?:[.!?]|\s|$)",
    re.IGNORECASE,
)
_VERIFY_EXIT_CODE = re.compile(
    r"\b(?:exit[_ ]?code|returncode)\s*[:=]\s*(-?\d+)\b", re.IGNORECASE
)


def _validation_rule_for_criterion(index: int, criterion: str) -> ValidationRule:
    """Map only explicit machine-checkable prose to deterministic checks."""
    normalized = criterion.strip()
    match = _EXPLICIT_CHECK.match(normalized)
    if match is not None:
        check, param = match.groups()
        return ValidationRule(
            id=f"criterion-{index}",
            description=normalized,
            kind="deterministic",
            check=check.lower(),
            param=param.strip(),
        )

    file_match = _FILE_EXISTS.search(normalized)
    if file_match is not None:
        return ValidationRule(
            id=f"criterion-{index}",
            description=normalized,
            kind="deterministic",
            check="file_exists",
            param=file_match.group(1),
        )

    return ValidationRule(
        id=f"criterion-{index}",
        description=normalized,
        kind="llm",
    )


def _execution_evidence(
    plan: PlanArtifact, report: ExecutionResult
) -> dict[str, object]:
    verify_output = getattr(report, "verify_output", "")
    context: dict[str, object] = {
        "verify_output": verify_output,
        "diff": getattr(report, "diff", ""),
        "artifacts": report.artifacts,
    }
    if isinstance(plan, ArchitectBrief) and isinstance(verify_output, str):
        exit_match = _VERIFY_EXIT_CODE.search(verify_output)
        if exit_match is not None:
            context["verify_commands"] = {
                plan.verify_cmd: {
                    "exit_code": int(exit_match.group(1)),
                    "output": verify_output,
                }
            }
    return context


_UNSET = object()


def _get_default_judge_llm() -> object | None:
    for getter in (get_executor_llm, get_architect_llm, get_router_llm):
        try:
            return getter()
        except Exception:
            continue
    return None


def _attach_self_check(
    report: ExecutionResult,
    plan: PlanArtifact,
    llm_judge: LlmJudge | None | object = _UNSET,
) -> ExecutionResult:
    """Attach M/N from report evidence without changing graph control flow."""
    if not plan.acceptance_criteria:
        return report

    rules = [
        _validation_rule_for_criterion(index, criterion)
        for index, criterion in enumerate(plan.acceptance_criteria, start=1)
        if criterion.strip()
    ]
    if not rules:
        return report

    context = _execution_evidence(plan, report)
    if llm_judge is _UNSET:
        judge = None
        has_semantic = any(rule.kind == "llm" for rule in rules)
        if has_semantic:
            llm_instance = _get_default_judge_llm()
            if llm_instance is not None:
                judge = build_llm_judge(llm_instance)
    else:
        judge = llm_judge

    return report.model_copy(
        update={
            "self_check": summarize(run_validation(rules, context, llm_judge=judge))
        }
    )


def _is_quota_or_rate_limit_error(exc: Exception) -> bool:
    text = str(exc).lower()
    quota_indicators = [
        "hit your usage limit",
        "usage limit",
        "rate limit",
        "purchase more credits",
        "too many requests",
        "insufficient_quota",
        "quota exceeded",
        "429",
    ]
    return any(ind in text for ind in quota_indicators)


def executor_node(state: AgentState) -> dict[str, ExecutorReport]:
    """
    R4 Executor node.
    Requires state["plan"] to be an ArchitectBrief.
    Executes the plan using the sandboxed executor agent.
    Returns ExecutorReport in executor_output.
    """
    plan = state.get("plan")
    if not isinstance(plan, (ArchitectBrief, PlanArtifact)):
        raise ValueError("Executor requires a PlanArtifact or ArchitectBrief plan.")

    user_message = f"Please execute this ArchitectBrief:\n{plan.model_dump_json()}"
    trimmed = trim_agent_messages(state.get("messages", []), user_message)

    llm_executor = os.getenv("LLM_EXECUTOR", "")
    if llm_executor.startswith("cli/"):
        backend = llm_executor[4:]

        invoker = build_cli_executor_invoker(backend)
        copied_state = dict(state)
        copied_state["messages"] = trimmed

        try:
            report = invoker(copied_state)
        except (CliBackendError, ValueError) as exc:
            fallback_backend = os.getenv(
                "LLM_EXECUTOR_FALLBACK",
                "claude-code" if backend == "codex" else "",
            )
            if fallback_backend and _is_quota_or_rate_limit_error(exc):
                fallback_name = (
                    fallback_backend[4:]
                    if fallback_backend.startswith("cli/")
                    else fallback_backend
                )
                try:
                    fallback_invoker = build_cli_executor_invoker(fallback_name)
                    report = fallback_invoker(copied_state)
                    return {"executor_output": _attach_self_check(report, plan)}
                except Exception as fallback_exc:
                    raise RuntimeError(
                        f"CLI executor '{backend}' hit quota limit and fallback '{fallback_name}' also failed: {fallback_exc}"
                    ) from fallback_exc

            raise RuntimeError(
                f"CLI executor failed ({type(exc).__name__}). Partial sandbox "
                "changes may exist and should be inspected before resume. "
                f"Details: {exc}"
            ) from exc

        return {"executor_output": _attach_self_check(report, plan)}

    agent = build_executor_agent()
    result = invoke_with_llm_retry(lambda: agent.invoke({"messages": trimmed}))

    if "structured_response" not in result or not isinstance(
        result["structured_response"], (ExecutorReport, ExecutionResult)
    ):
        raise ValueError(
            "Executor agent failed to return a valid ExecutionResult or ExecutorReport."
        )

    return {"executor_output": _attach_self_check(result["structured_response"], plan)}
