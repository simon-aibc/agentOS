"""Semantic LLM Judge for post-execution acceptance criteria evaluation.

Evaluates natural language acceptance criteria against independent execution
evidence (verify_output, diff, artifacts) using an injected LLM.
Strictly evidence-bounded, token-capped, and fail-closed with timeout.
"""

from __future__ import annotations

import concurrent.futures
import logging
import re
from collections.abc import Mapping
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from agent_os.validation import LlmJudge, ValidationRule

logger = logging.getLogger(__name__)

_JUDGE_SYSTEM_PROMPT = """You are an objective acceptance criteria evaluator.
Your role is to evaluate whether a specific acceptance criterion is met based STRICTLY AND ONLY on the provided execution evidence.

Rules:
1. ONLY rely on the provided evidence (verify_output, diff, artifacts).
2. Do NOT invent, assume, or extrapolate facts not directly present in the evidence.
3. If the evidence lacks proof or is ambiguous, you must judge FAIL.
4. Format:
   First line MUST begin with either "PASS" or "FAIL".
   Second line should provide a concise 1-sentence factual justification based on the evidence.
"""


def _format_evidence_context(context: Mapping[str, object]) -> str:
    parts = []
    verify_output = context.get("verify_output")
    if verify_output:
        parts.append(f"=== Verify Command Output ===\n{verify_output}")

    diff = context.get("diff")
    if diff:
        parts.append(f"=== Code / File Diff ===\n{diff}")

    artifacts = context.get("artifacts")
    if artifacts:
        if isinstance(artifacts, (list, tuple)):
            art_str = "\n".join(f"- {a}" for a in artifacts)
        else:
            art_str = str(artifacts)
        parts.append(f"=== Generated Artifacts ===\n{art_str}")

    if not parts:
        return "(No execution evidence provided)"
    return "\n\n".join(parts)


def _parse_judge_response(response_text: str) -> tuple[bool, str]:
    """Extract PASS/FAIL and reasoning from LLM response."""
    lines = [
        line.strip() for line in response_text.strip().splitlines() if line.strip()
    ]
    if not lines:
        return False, "Judge returned empty response."

    first_line = lines[0].upper()
    reasoning = lines[1] if len(lines) > 1 else lines[0]

    if re.match(r"^PASS\b", first_line):
        return True, reasoning
    elif re.match(r"^FAIL\b", first_line):
        return False, reasoning

    return False, f"Indeterminate judge response: {lines[0]}"


def build_llm_judge(
    llm: Any,
    *,
    timeout_s: float = 20.0,
    max_tokens: int = 256,
) -> LlmJudge:
    """Construct an evidence-bounded, token-capped LlmJudge with timeout safety."""
    bound_llm = llm
    has_bind = (
        hasattr(type(llm), "bind")
        or "bind" in getattr(llm, "_mock_children", {})
        or "bind" in getattr(llm, "__dict__", {})
    )
    if has_bind:
        try:
            bound_llm = llm.bind(max_tokens=max_tokens)
        except Exception:
            try:
                bound_llm = llm.bind(max_output_tokens=max_tokens)
            except Exception:
                bound_llm = llm

    def judge(
        rule: ValidationRule, evidence_context: Mapping[str, object]
    ) -> tuple[bool, str]:
        evidence_text = _format_evidence_context(evidence_context)
        user_prompt = f"""Evaluate this acceptance criterion:
Criterion ID: {rule.id}
Description: {rule.description}
Parameter: {rule.param or "(none)"}

Execution Evidence:
{evidence_text}
"""
        messages = [
            SystemMessage(content=_JUDGE_SYSTEM_PROMPT),
            HumanMessage(content=user_prompt),
        ]

        def _invoke() -> str:
            if hasattr(bound_llm, "invoke"):
                response = bound_llm.invoke(messages)
                content = (
                    response.content if hasattr(response, "content") else str(response)
                )
            elif callable(bound_llm):
                response = bound_llm(messages)
                content = (
                    response.content if hasattr(response, "content") else str(response)
                )
            else:
                raise TypeError("LLM object is neither callable nor an invoker.")

            if isinstance(content, list):
                text_parts = [
                    part.get("text", "") if isinstance(part, dict) else str(part)
                    for part in content
                ]
                content = " ".join(text_parts)
            return str(content)

        # Manual executor (not `with`): a `with` block calls shutdown(wait=True)
        # on exit, which would block on a hung _invoke thread even after the
        # timeout fired — defeating the timeout. shutdown(wait=False) abandons it.
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        future = executor.submit(_invoke)
        try:
            raw_content = future.result(timeout=timeout_s)
        except concurrent.futures.TimeoutError:
            logger.warning("LLM judge evaluation timed out after %.1fs", timeout_s)
            return False, "LLM judge timeout"
        except Exception as exc:
            logger.warning("LLM judge evaluation failed: %s", exc)
            return False, f"LLM judge execution error: {type(exc).__name__}"
        finally:
            executor.shutdown(wait=False)

        return _parse_judge_response(raw_content)

    return judge
