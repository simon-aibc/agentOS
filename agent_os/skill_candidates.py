"""Deterministic, read-only proposals for worthwhile new skills.

This module detects repeated, explicitly-labelled task shapes. It only
proposes a candidate; it has no authority to author, register, or execute a
skill.
"""

from __future__ import annotations

import re
from typing import Final

from pydantic import BaseModel, ConfigDict

from agent_os.observations import SqliteObservationStore, TaskSignaturePattern
from agent_os.skills import SkillRegistry

MIN_SKILL_CANDIDATE_OCCURRENCES: Final = 3
MIN_SKILL_CANDIDATE_OUTCOME_SCORE: Final = 0.75
MAX_SAMPLE_RUN_IDS: Final = 3
_SAFE_NAME_PART = re.compile(r"[^a-z0-9]+")


class SkillCandidate(BaseModel):
    """A reviewable proposal, never an executable or registered capability."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    signature: str
    task_kind: str
    occurrence_count: int
    success_count: int
    outcome_score: float
    sample_run_ids: tuple[str, ...]
    suggested_skill_name: str
    rationale: str


def suggested_skill_name(pattern: TaskSignaturePattern) -> str:
    """Derive a stable, safe candidate name without reversing the task hash."""
    kind = _SAFE_NAME_PART.sub("-", pattern.task_kind.lower()).strip("-") or "workflow"
    digest = pattern.task_signature.removeprefix("sha256:v1:")
    return f"candidate-{kind}-{digest[:12]}"


def _is_covered(registry: SkillRegistry, name: str) -> bool:
    return (
        registry.get(name) is not None or registry.deterministic_match(name) is not None
    )


def _candidate(pattern: TaskSignaturePattern) -> SkillCandidate:
    name = suggested_skill_name(pattern)
    score = round(pattern.outcome_score, 4)
    return SkillCandidate(
        signature=pattern.task_signature,
        task_kind=pattern.task_kind,
        occurrence_count=pattern.occurrence_count,
        success_count=pattern.accepted_count,
        outcome_score=score,
        sample_run_ids=pattern.sample_run_ids,
        suggested_skill_name=name,
        rationale=(
            f"{pattern.occurrence_count} labelled {pattern.task_kind} occurrences; "
            f"{pattern.accepted_count} accepted; outcome score {score:.2f}; "
            "no registered exact candidate skill."
        ),
    )


def mine_skill_candidates(
    store: SqliteObservationStore | None,
    registry: SkillRegistry,
    *,
    workspace_id: str,
    min_occurrences: int = MIN_SKILL_CANDIDATE_OCCURRENCES,
    min_outcome_score: float = MIN_SKILL_CANDIDATE_OUTCOME_SCORE,
) -> tuple[SkillCandidate, ...]:
    """Return stable candidate proposals from explicit historical outcomes only."""
    if store is None:
        return ()
    if min_occurrences < 1:
        raise ValueError("min_occurrences must be positive")
    if not 0.0 <= min_outcome_score <= 1.0:
        raise ValueError("min_outcome_score must be between 0 and 1")

    candidates = []
    for pattern in store.aggregate_task_signatures(
        workspace_id=workspace_id,
        sample_limit=MAX_SAMPLE_RUN_IDS,
    ):
        if pattern.occurrence_count < min_occurrences:
            continue
        if pattern.outcome_score < min_outcome_score:
            continue
        candidate = _candidate(pattern)
        if _is_covered(registry, candidate.suggested_skill_name):
            continue
        candidates.append(candidate)
    return tuple(
        sorted(
            candidates,
            key=lambda item: (
                -item.occurrence_count,
                -item.outcome_score,
                item.suggested_skill_name,
                item.signature,
            ),
        )
    )
