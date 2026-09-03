from pathlib import Path

import pytest
from pydantic import ValidationError

from agent_os.observations import SqliteObservationStore, task_signature_for_input
from agent_os.skill_candidates import (
    MIN_SKILL_CANDIDATE_OCCURRENCES,
    SkillCandidate,
    mine_skill_candidates,
    suggested_skill_name,
)
from agent_os.skills import RegisteredSkill, SkillRegistry


def _store(tmp_path: Path) -> SqliteObservationStore:
    return SqliteObservationStore(str(tmp_path / "observations.db"))


def _signature(label: str) -> str:
    signature = task_signature_for_input(f"repeatable {label} task")
    assert signature is not None
    return signature


def _record(
    store: SqliteObservationStore,
    *,
    signature: str | None,
    signal: str,
    index: int,
    workspace: str = "workspace-a",
    task_kind: str = "workflow",
) -> None:
    observation = store.create(
        workspace_id=workspace,
        run_id=f"{workspace}-{task_kind}-{index}",
        thread_id=f"thread-{index}",
        task_kind=task_kind,
        task_signature=signature,
        approach="default-v1",
        outcome_signal="unknown",
        source="server.run",
    )
    store.record_outcome(
        observation.observation_id, signal=signal, evidence="operator label"
    )


def _seed(
    store: SqliteObservationStore,
    *,
    signature: str,
    accepted: int,
    rejected: int = 0,
    edited: int = 0,
    workspace: str = "workspace-a",
    task_kind: str = "workflow",
) -> None:
    index = 0
    for signal, count in (
        ("accepted", accepted),
        ("rejected", rejected),
        ("edited", edited),
    ):
        for _ in range(count):
            _record(
                store,
                signature=signature,
                signal=signal,
                index=index,
                workspace=workspace,
                task_kind=task_kind,
            )
            index += 1


def test_mine_skill_candidates_returns_empty_for_empty_or_legacy_store(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    _record(store, signature=None, signal="accepted", index=1)
    store.create(
        workspace_id="workspace-a",
        run_id="unknown-signed-run",
        thread_id="unknown-signed-thread",
        task_kind="workflow",
        task_signature=_signature("unknown-only"),
        approach="default-v1",
        outcome_signal="unknown",
        source="server.run",
    )

    assert (
        mine_skill_candidates(store, SkillRegistry(), workspace_id="workspace-a") == ()
    )
    assert (
        mine_skill_candidates(None, SkillRegistry(), workspace_id="workspace-a") == ()
    )


def test_miner_requires_both_occurrence_and_outcome_score_thresholds(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    _seed(
        store,
        signature=_signature("too-few"),
        accepted=MIN_SKILL_CANDIDATE_OCCURRENCES - 1,
    )
    _seed(store, signature=_signature("low-score"), accepted=2, rejected=1)
    _seed(store, signature=_signature("eligible"), accepted=3)

    candidates = mine_skill_candidates(
        store, SkillRegistry(), workspace_id="workspace-a"
    )

    assert len(candidates) == 1
    assert candidates[0].signature == _signature("eligible")
    assert candidates[0].occurrence_count == candidates[0].success_count == 3
    assert candidates[0].outcome_score == 1.0


def test_miner_is_workspace_signature_isolated_and_does_not_write(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    signature = _signature("isolated")
    _seed(store, signature=signature, accepted=3, workspace="workspace-a")
    _seed(store, signature=signature, accepted=5, workspace="workspace-b")
    before = [item.to_dict() for item in store.list(workspace_id="workspace-a")]

    candidates = mine_skill_candidates(
        store, SkillRegistry(), workspace_id="workspace-a"
    )

    assert len(candidates) == 1
    assert candidates[0].occurrence_count == 3
    assert [item.to_dict() for item in store.list(workspace_id="workspace-a")] == before


def test_miner_excludes_exact_registry_coverage(tmp_path: Path) -> None:
    store = _store(tmp_path)
    signature = _signature("covered")
    _seed(store, signature=signature, accepted=3)
    pattern = store.aggregate_task_signatures(workspace_id="workspace-a")[0]
    registry = SkillRegistry()
    registry.register(
        RegisteredSkill(
            name=suggested_skill_name(pattern), aliases=(), handler=lambda: None
        )
    )

    assert mine_skill_candidates(store, registry, workspace_id="workspace-a") == ()


def test_skill_candidates_api_returns_read_only_workspace_candidates(
    tmp_path: Path, monkeypatch
) -> None:
    from agent_os.server import api

    store = _store(tmp_path)
    signature = _signature("api-candidate")
    _seed(store, signature=signature, accepted=3)
    monkeypatch.setattr(
        api,
        "_runtime_skill_candidate_context",
        lambda: (store, "workspace-a", SkillRegistry()),
    )

    payload = api.list_skill_candidates_endpoint()

    assert payload["workspace_id"] == "workspace-a"
    assert payload["candidates"] == [
        {
            "signature": signature,
            "task_kind": "workflow",
            "occurrence_count": 3,
            "success_count": 3,
            "outcome_score": 1.0,
            "sample_run_ids": [
                "workspace-a-workflow-2",
                "workspace-a-workflow-1",
                "workspace-a-workflow-0",
            ],
            "suggested_skill_name": suggested_skill_name(
                store.aggregate_task_signatures(workspace_id="workspace-a")[0]
            ),
            "rationale": "3 labelled workflow occurrences; 3 accepted; outcome score 1.00; no registered exact candidate skill.",
        }
    ]


def test_miner_sorting_samples_and_candidate_model_are_stable(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first = _signature("alpha")
    second = _signature("beta")
    _seed(store, signature=second, accepted=3)
    _seed(store, signature=first, accepted=3)

    candidates = mine_skill_candidates(
        store, SkillRegistry(), workspace_id="workspace-a"
    )

    assert tuple(item.suggested_skill_name for item in candidates) == tuple(
        sorted(item.suggested_skill_name for item in candidates)
    )
    assert all(len(item.sample_run_ids) == 3 for item in candidates)
    assert "outcome score 1.00" in candidates[0].rationale
    with pytest.raises(ValidationError):
        SkillCandidate(**candidates[0].model_dump(), unexpected=True)
    with pytest.raises(ValidationError):
        candidates[0].occurrence_count = 99
