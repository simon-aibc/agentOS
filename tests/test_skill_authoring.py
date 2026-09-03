from __future__ import annotations

from pathlib import Path

import pytest

from agent_os.skill_authoring import (
    CandidateAuthoringInput,
    SkillAuthoringError,
    SkillAuthoringService,
    package_digest,
)
from agent_os.skill_packages import SkillPackageLoader
from agent_os.skills import RegisteredSkill, SkillRegistry
from agent_os.workspace import compose_workspace, load_workspace


def _workspace(root: Path) -> Path:
    (root / "SYSTEM.md").write_text("# system\n")
    (root / "CONTEXT.md").write_text("# context\n")
    path = root / "workspace.toml"
    path.write_text(
        "\n".join(
            [
                "skills = []",
                'connectors = ["filesystem", "memory"]',
                "",
                "[workspace]",
                'name = "skill-authoring-test"',
                "",
                "[backends]",
                'architect = "codex"',
                'executor = "codex"',
                "",
                "[memory]",
                'type = "markdown"',
                'path = "."',
                "",
                "[context]",
                'sources = ["SYSTEM.md", "CONTEXT.md"]',
                "",
                "[policy]",
                'mode = "local"',
            ]
        )
    )
    return path


def _candidate() -> CandidateAuthoringInput:
    return CandidateAuthoringInput(
        candidate_id="candidate-1",
        signature="workflow:normalize-email",
        task_kind="workflow",
        occurrence_count=6,
        success_count=5,
        outcome_score=0.8,
        sample_run_ids=("run-1", "run-2"),
        suggested_skill_name="email_normalizer",
        rationale="Repeated accepted workflow with deterministic inputs.",
    )


def _human_draft(root: Path, *, broken: bool = False) -> Path:
    package = root / "skill-drafts" / "email_normalizer"
    package.mkdir(parents=True)
    (package / "manifest.toml").write_text(
        "\n".join(
            [
                "[skill]",
                'name = "email_normalizer"',
                'version = "0.1.0"',
                "",
                "[[skill.handlers]]",
                'match = ["email_normalizer", "normalize_email"]',
                'entrypoint = "handlers:email_normalizer"',
            ]
        )
    )
    (package / "handlers.py").write_text(
        "def email_normalizer(task: str = '', **kwargs):\n"
        + (
            "    this is invalid python!\n"
            if broken
            else "    return {'ok': True, 'task': task}\n"
        )
    )
    return package


def _service(root: Path) -> tuple[SkillAuthoringService, Path]:
    config = _workspace(root)
    return SkillAuthoringService(load_workspace(config)), config


def test_prepare_brief_is_immutable_and_has_no_authoring_side_effect(
    tmp_path: Path,
) -> None:
    service, config = _service(tmp_path)

    request = service.prepare_brief(_candidate(), now=100)

    assert request.status == "brief_ready"
    assert request.brief["candidate_id"] == "candidate-1"
    assert "raw_task" not in request.brief
    assert not (tmp_path / "skill-packages").exists()
    assert "skill-packages" not in config.read_text()
    with pytest.raises(SkillAuthoringError, match="immutable"):
        service.prepare_brief(_candidate(), now=101)


def test_failed_draft_preserves_workspace_config_and_registry(tmp_path: Path) -> None:
    service, config = _service(tmp_path)
    request = service.prepare_brief(_candidate(), now=100)
    draft = _human_draft(tmp_path, broken=True)
    original = config.read_text()

    result = service.submit_draft(request.request_id, str(draft), now=101)

    assert result.status == "validation_failed"
    assert result.validation["total"] == 2
    assert result.validation["met"] < result.validation["total"]
    assert config.read_text() == original
    assert not (tmp_path / "skill-packages").exists()
    assert (
        compose_workspace(load_workspace(config)).skill_registry.get("email_normalizer")
        is None
    )


def test_validated_draft_requires_final_confirmation_and_then_fresh_compose_loads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, config = _service(tmp_path)
    monkeypatch.setenv("AGENT_OS_WORKSPACE", str(config))
    from agent_os.server import runtime

    runtime.composed_workspace.cache_clear()
    request = service.prepare_brief(_candidate(), now=100)
    draft = _human_draft(tmp_path)
    validated = service.submit_draft(request.request_id, str(draft), now=101)
    assert validated.status == "validated"
    assert validated.validation == {
        "total": 2,
        "met": 2,
        "results": validated.validation["results"],
    }
    with pytest.raises(SkillAuthoringError, match="confirm=true"):
        service.confirm_registration(request.request_id, confirm=False, now=102)
    assert "skill-packages" not in config.read_text()

    registered = service.confirm_registration(request.request_id, confirm=True, now=103)

    assert registered.status == "registered"
    assert registered.registration_confirmed_at == 103
    assert registered.registration_result["registered_skill_name"] == "email_normalizer"
    assert (
        tmp_path / "skill-packages" / "email_normalizer" / "manifest.toml"
    ).is_file()
    assert (
        compose_workspace(load_workspace(config)).skill_registry.get("email_normalizer")
        is not None
    )
    runtime.composed_workspace.cache_clear()


def test_changed_draft_cannot_cross_the_second_confirmation(tmp_path: Path) -> None:
    service, config = _service(tmp_path)
    request = service.prepare_brief(_candidate(), now=100)
    draft = _human_draft(tmp_path)
    service.submit_draft(request.request_id, str(draft), now=101)
    (draft / "handlers.py").write_text(
        "def email_normalizer(**kwargs):\n    return {'changed': True}\n"
    )

    with pytest.raises(SkillAuthoringError, match="changed after validation"):
        service.confirm_registration(request.request_id, confirm=True, now=102)

    assert "skill-packages" not in config.read_text()
    assert service.store.get(request.request_id).status == "validated"
    assert package_digest(draft) != service.store.get(request.request_id).draft_digest


def test_cancelled_request_is_inert(tmp_path: Path) -> None:
    service, config = _service(tmp_path)
    request = service.prepare_brief(_candidate(), now=100)

    cancelled = service.cancel(request.request_id)

    assert cancelled.status == "cancelled"
    assert "skill-packages" not in config.read_text()
    with pytest.raises(SkillAuthoringError, match="not ready"):
        service.submit_draft(
            request.request_id, str(tmp_path / "skill-drafts"), now=101
        )


def test_package_loader_batch_collision_does_not_partially_register(
    tmp_path: Path,
) -> None:
    package = tmp_path / "package"
    package.mkdir()
    (package / "manifest.toml").write_text(
        """[skill]
name = "bundle"
version = "0.1.0"

[[skill.handlers]]
match = ["new_skill"]
entrypoint = "handlers:new_skill"

[[skill.handlers]]
match = ["taken"]
entrypoint = "handlers:taken"
"""
    )
    (package / "handlers.py").write_text(
        "def new_skill(**kwargs): return {}\ndef taken(**kwargs): return {}\n"
    )
    registry = SkillRegistry()
    registry.register(RegisteredSkill(name="taken", aliases=(), handler=lambda: None))

    with pytest.raises(ValueError, match="already registered"):
        SkillPackageLoader(registry).load_package(package)

    assert registry.names() == ["taken"]


def test_authoring_api_only_exposes_explicit_gate_actions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_os.server import api

    service, _config = _service(tmp_path)
    monkeypatch.setattr(api, "_runtime_skill_authoring_service", lambda: service)
    created = api.prepare_skill_authoring_request_endpoint(
        api.PrepareSkillAuthoringRequest(candidate=_candidate())
    )
    request_id = created["request_id"]

    assert created["status"] == "brief_ready"
    assert (
        api.list_skill_authoring_requests_endpoint()["requests"][0]["request_id"]
        == request_id
    )
    cancelled = api.cancel_skill_authoring_request_endpoint(request_id)
    assert cancelled["status"] == "cancelled"
