"""Fixed, evidence-bounded architect strategy selection.

The selector is deliberately deterministic. It never creates a strategy from
model text and has no authority over policy, permissions, or tool execution.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict

from agent_os.observations import OutcomePattern, SqliteObservationStore

Confidence = Literal["insufficient", "exploratory", "actionable"]

MIN_LABELLED_PER_STRATEGY: Final = 5
MIN_ACTIONABLE_SCORE: Final = 0.70
MIN_ACTIONABLE_LEAD: Final = 0.15
SELECTOR_VERSION: Final = "v1.0"


@dataclass(frozen=True)
class StrategyDefinition:
    strategy_id: str
    version: int
    task_kind: str
    directive: str


class StrategyHint(BaseModel):
    """Checkpoint-safe structured hint rendered at the architect boundary."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    strategy_id: str
    version: int
    task_kind: str
    selection_reason: Literal["explicit", "evidence_backed", "exploration", "default"]
    directive: str


@dataclass(frozen=True)
class StrategySelection:
    hint: StrategyHint
    patterns: tuple[OutcomePattern, ...]


_WORKFLOW_STRATEGIES: Final = (
    StrategyDefinition(
        strategy_id="default-v1",
        version=1,
        task_kind="workflow",
        directive=(
            "Create a clear direct plan, identify material assumptions, and include "
            "necessary validation without extra exploration."
        ),
    ),
    StrategyDefinition(
        strategy_id="verification-first-v1",
        version=1,
        task_kind="workflow",
        directive=(
            "Inspect relevant constraints and files before proposing a plan; identify "
            "risks and unknowns, favor minimal reversible changes, and include explicit "
            "validation plus rollback or safety checkpoints."
        ),
    ),
    StrategyDefinition(
        strategy_id="concise-plan-v1",
        version=1,
        task_kind="workflow",
        directive=(
            "Produce only necessary ordered steps, the top risks, and exact validation; "
            "avoid optional discovery or expanded scope."
        ),
    ),
    StrategyDefinition(
        strategy_id="clarify-first-v1",
        version=1,
        task_kind="workflow",
        directive=(
            "Before proposing a plan or taking any action, surface 3-5 binary "
            "(pass/fail) acceptance criteria that define 'done' for this task and "
            "place them in the structured acceptance_criteria field as concise statements. "
            "Classify each intended side-effect as reversible or irreversible. "
            "Present these to the human for confirmation or adjustment first; do not "
            "execute irreversible side-effects until criteria are confirmed. This "
            "directive cannot change permission, tool-safety, or execution constraints."
        ),
    ),
)

_REGISTRY: Final = {"workflow": _WORKFLOW_STRATEGIES}


def strategies_for(task_kind: str) -> tuple[StrategyDefinition, ...]:
    return _REGISTRY.get(task_kind, ())


def _hint(
    definition: StrategyDefinition,
    reason: Literal["explicit", "evidence_backed", "exploration", "default"],
) -> StrategyHint:
    return StrategyHint(
        strategy_id=definition.strategy_id,
        version=definition.version,
        task_kind=definition.task_kind,
        selection_reason=reason,
        directive=definition.directive,
    )


def _evidence_winner(
    patterns: tuple[OutcomePattern, ...],
) -> tuple[OutcomePattern, OutcomePattern] | None:
    eligible = [
        pattern
        for pattern in patterns
        if pattern.labelled_count >= MIN_LABELLED_PER_STRATEGY
    ]
    if len(eligible) < 2:
        return None
    ranked = sorted(eligible, key=lambda item: (-item.outcome_score, item.strategy_id))
    winner, comparator = ranked[:2]
    if winner.outcome_score < MIN_ACTIONABLE_SCORE:
        return None
    if winner.outcome_score - comparator.outcome_score < MIN_ACTIONABLE_LEAD:
        return None
    return winner, comparator


def select_strategy(
    store: SqliteObservationStore | None,
    *,
    workspace_id: str,
    task_kind: str,
    run_id: str | None,
    explicit_strategy_id: str | None = None,
) -> StrategySelection:
    """Return a reproducible, fixed strategy without interpreting raw evidence."""
    definitions = strategies_for(task_kind)
    fallback = _WORKFLOW_STRATEGIES[0]
    if not definitions:
        return StrategySelection(_hint(fallback, "default"), ())
    by_id = {item.strategy_id: item for item in definitions}
    if explicit_strategy_id is not None and explicit_strategy_id not in by_id:
        raise ValueError(
            f"Strategy '{explicit_strategy_id}' is not allowed for task kind '{task_kind}'"
        )
    if store is None or run_id is None:
        return StrategySelection(
            _hint(
                by_id.get(explicit_strategy_id, fallback),
                "explicit" if explicit_strategy_id else "default",
            ),
            (),
        )

    existing = store.get_strategy_assignment(run_id)
    if existing is not None:
        definition = by_id.get(existing.strategy_id)
        if definition is not None:
            return StrategySelection(
                _hint(definition, existing.selection_reason),
                (),
            )

    if explicit_strategy_id is not None:
        defn = by_id[explicit_strategy_id]
        strategy_id = store.assign_strategy(
            workspace_id=workspace_id,
            task_kind=task_kind,
            run_id=run_id,
            strategy_id=explicit_strategy_id,
            strategy_version=defn.version,
            selection_reason="explicit",
            selector_version=SELECTOR_VERSION,
            evidence_summary=None,
        )
        return StrategySelection(_hint(by_id[strategy_id], "explicit"), ())

    patterns = tuple(
        store.aggregate_patterns(
            workspace_id=workspace_id,
            task_kind=task_kind,
            strategy_ids=tuple(by_id),
        )
    )
    winner_match = _evidence_winner(patterns)
    if winner_match is not None:
        winner, second = winner_match
        defn = by_id[winner.strategy_id]
        evidence_summary = {
            "labelled_counts": {p.strategy_id: p.labelled_count for p in patterns},
            "scores": {p.strategy_id: round(p.outcome_score, 4) for p in patterns},
            "winner": winner.strategy_id,
            "threshold_met": True,
            "margin": round(winner.outcome_score - second.outcome_score, 4),
        }
        strategy_id = store.assign_strategy(
            workspace_id=workspace_id,
            task_kind=task_kind,
            run_id=run_id,
            strategy_id=winner.strategy_id,
            strategy_version=defn.version,
            selection_reason="evidence_backed",
            selector_version=SELECTOR_VERSION,
            evidence_summary=evidence_summary,
        )
        return StrategySelection(_hint(by_id[strategy_id], "evidence_backed"), patterns)

    strategy_id = store.assign_balanced_exploration(
        workspace_id=workspace_id,
        task_kind=task_kind,
        run_id=run_id,
        strategy_ids=tuple(by_id),
        strategy_versions={item.strategy_id: item.version for item in definitions},
        selector_version=SELECTOR_VERSION,
    )
    return StrategySelection(_hint(by_id[strategy_id], "exploration"), patterns)
