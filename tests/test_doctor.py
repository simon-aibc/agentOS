import json
from collections.abc import Callable
from unittest.mock import MagicMock

import pytest

from agent_os.backends import AntigravityAdapter, AuthStatus, BackendRegistry
from agent_os.cli.doctor import run_doctor


class MockAdapter:
    name = "mock"
    binary_name = "mock-bin"
    supported_roles = frozenset({"architect", "executor"})
    stub = False

    def build_invoker(self, role: str) -> Callable:
        return lambda state: None

    def authentication_status(self) -> AuthStatus:
        return AuthStatus(status="ok", detail="all good")


@pytest.fixture(autouse=True)
def setup_doctor_env(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_ARCHITECT", "cli/mock")
    monkeypatch.setenv("LLM_EXECUTOR", "cli/mock")
    monkeypatch.setenv("AGENT_OS_SANDBOX", str(tmp_path / "sandbox"))
    monkeypatch.setenv("AGENT_OS_CHECKPOINTS_DB", str(tmp_path / "checkpoints.db"))

    # Mock registry

    def fake_registry():
        reg = BackendRegistry()
        reg.register(MockAdapter())
        return reg

    monkeypatch.setattr("agent_os.cli.doctor.build_default_registry", fake_registry)

    # Mock shutil.which
    monkeypatch.setattr(
        "shutil.which", lambda bin: f"/fake/{bin}" if bin == "mock-bin" else None
    )


def test_doctor_healthy(tmp_path):
    exit_code, output = run_doctor(json_output=True)
    assert exit_code == 0, output
    data = json.loads(output)

    assert data["checkpoints_db"]["writable"] is True
    assert data["checkpoints_db"]["exists"] is False
    assert not data["warnings"]
    assert list(data.keys()) == [
        "registered_adapters",
        "resolved_config",
        "profile",
        "profile_source",
        "checkpoints_db",
        "backend_binding",
        "warnings",
    ]
    assert data["registered_adapters"][0]["stub"] is False
    assert data["backend_binding"] == {
        "router": data["resolved_config"]["router"],
        "architect": "cli/mock",
        "executor": "cli/mock",
        "profile_name": None,
        "sandbox_root": data["resolved_config"]["sandbox"],
    }


def test_doctor_uses_workspace_backend_binding(monkeypatch, tmp_path):
    from agent_os.backends import build_default_registry as real_registry

    monkeypatch.setattr("agent_os.cli.doctor.build_default_registry", real_registry)
    monkeypatch.setattr("shutil.which", lambda binary: f"/fake/{binary}")
    workspace = tmp_path / "workspace.toml"
    workspace.write_text(
        """
[workspace]
name = "doctor-workspace"

[backends]
architect = "codex"
executor = "codex"
""".strip(),
        encoding="utf-8",
    )

    exit_code, output = run_doctor(json_output=True, workspace_path=str(workspace))

    assert exit_code == 0, output
    data = json.loads(output)
    assert data["resolved_config"]["architect"] == "cli/codex"
    assert data["resolved_config"]["executor"] == "cli/codex"
    assert data["profile_source"] == "workspace"


def test_doctor_missing_binary(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda bin: None)
    exit_code, output = run_doctor(json_output=True)
    assert exit_code == 1
    data = json.loads(output)

    assert "binary is missing" in data["warnings"][0]
    assert data["registered_adapters"][0]["auth_status"]["status"] == "unknown"


def test_doctor_unregistered_backend(monkeypatch):
    monkeypatch.setenv("LLM_ARCHITECT", "cli/unknown-bin")
    exit_code, output = run_doctor(json_output=True)
    assert exit_code == 1
    data = json.loads(output)
    assert any("not registered" in w for w in data["warnings"])


def test_doctor_unsupported_role(monkeypatch):

    class ArchitectOnly(MockAdapter):
        supported_roles = frozenset({"architect"})

    def fake_registry():
        reg = BackendRegistry()
        reg.register(ArchitectOnly())
        return reg

    monkeypatch.setattr("agent_os.cli.doctor.build_default_registry", fake_registry)

    exit_code, output = run_doctor(json_output=True)
    assert exit_code == 1
    data = json.loads(output)
    assert any("does not support role" in w for w in data["warnings"])


def test_doctor_unauthenticated(monkeypatch):

    class Unauth(MockAdapter):
        def authentication_status(self) -> AuthStatus:
            return AuthStatus(status="unauthenticated", detail="login required")

    def fake_registry():
        reg = BackendRegistry()
        reg.register(Unauth())
        return reg

    monkeypatch.setattr("agent_os.cli.doctor.build_default_registry", fake_registry)

    exit_code, output = run_doctor(json_output=True)
    assert exit_code == 1
    data = json.loads(output)
    assert any("is unauthenticated" in w for w in data["warnings"])


def test_doctor_unknown_auth_is_warning_only(monkeypatch):

    class UnknownAuth(MockAdapter):
        def authentication_status(self) -> AuthStatus:
            return AuthStatus(status="unknown", detail="idk")

    def fake_registry():
        reg = BackendRegistry()
        reg.register(UnknownAuth())
        return reg

    monkeypatch.setattr("agent_os.cli.doctor.build_default_registry", fake_registry)

    exit_code, output = run_doctor(json_output=True)
    assert exit_code == 0
    data = json.loads(output)
    assert any("auth status is unknown" in w for w in data["warnings"])


def test_doctor_unwritable_checkpoint_path(monkeypatch, tmp_path):
    ro_dir = tmp_path / "ro"
    ro_dir.mkdir()
    ro_dir.chmod(0o500)

    monkeypatch.setenv("AGENT_OS_CHECKPOINTS_DB", str(ro_dir / "checkpoints.db"))

    try:
        exit_code, output = run_doctor(json_output=True)
        assert exit_code == 1
        data = json.loads(output)
        assert data["checkpoints_db"]["writable"] is False
        assert any("not writable" in w for w in data["warnings"])
    finally:
        ro_dir.chmod(0o700)


def test_doctor_human_output():
    exit_code, output = run_doctor(json_output=False)
    assert exit_code == 0
    assert "Registered adapters" in output
    assert "Resolved config" in output
    assert "Profile: none" in output
    assert "Checkpoints DB" in output
    assert "Backend binding" in output
    assert "Warnings" in output
    assert "STATUS: OK" in output


def _registry_with_antigravity():
    registry = BackendRegistry()
    registry.register(MockAdapter())
    registry.register(AntigravityAdapter())
    return registry


def test_doctor_reports_antigravity_candidate_without_binary_probe(monkeypatch):
    monkeypatch.setattr(
        "agent_os.cli.doctor.build_default_registry",
        _registry_with_antigravity,
    )
    which = MagicMock(side_effect=lambda name: f"/fake/{name}")
    monkeypatch.setattr("shutil.which", which)

    exit_code, raw_json = run_doctor(json_output=True)
    data = json.loads(raw_json)
    candidate = next(
        item for item in data["registered_adapters"] if item["name"] == "antigravity"
    )

    assert exit_code == 0
    assert candidate["stub"] is True
    assert candidate["binary_path"] is None
    assert candidate["supported_roles"] == ["architect", "executor"]
    assert candidate["auth_status"]["status"] == "unknown"
    assert all(call.args[0] != "antigravity" for call in which.call_args_list)

    _, human_output = run_doctor(json_output=False)
    assert "Candidate adapters (not-yet-supported)" in human_output
    assert "- antigravity" in human_output


def test_configured_antigravity_is_warning_not_failure(monkeypatch):
    monkeypatch.setattr(
        "agent_os.cli.doctor.build_default_registry",
        _registry_with_antigravity,
    )
    monkeypatch.setenv("LLM_ARCHITECT", "cli/antigravity")

    exit_code, output = run_doctor(json_output=False)

    assert exit_code == 0
    assert "not-yet-supported stub" in output
    assert "STATUS: OK" in output


def test_configured_antigravity_does_not_mask_other_failure(monkeypatch):
    monkeypatch.setattr(
        "agent_os.cli.doctor.build_default_registry",
        _registry_with_antigravity,
    )
    monkeypatch.setenv("LLM_ARCHITECT", "cli/antigravity")
    monkeypatch.setenv("LLM_EXECUTOR", "cli/missing")

    exit_code, output = run_doctor(json_output=False)

    assert exit_code == 1
    assert "not-yet-supported stub" in output
    assert "not registered" in output
    assert "STATUS: FAIL" in output


def test_doctor_reports_effective_profile_config(monkeypatch, tmp_path):
    config_root = tmp_path / "xdg" / "agent-os"
    config_root.mkdir(parents=True)
    (config_root / "profiles.toml").write_text(
        'default = "profile"\n\n'
        "[profile.profile]\n"
        'router = "ollama/profile"\n'
        'sandbox = "./profile-sandbox"\n',
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_root.parent))
    monkeypatch.delenv("AGENT_OS_PROFILE", raising=False)
    monkeypatch.setenv("LLM_ROUTER", "old/router")
    monkeypatch.setenv("LLM_ARCHITECT", "old/architect")
    monkeypatch.setenv("LLM_EXECUTOR", "old/executor")

    exit_code, output = run_doctor(json_output=True)
    data = json.loads(output)

    assert exit_code == 0
    assert data["profile"] == "profile"
    assert data["profile_source"] == "default"
    assert data["resolved_config"]["router"] == "ollama/profile"
    assert data["resolved_config"]["architect"] is None
    assert data["resolved_config"]["executor"] is None
    assert data["resolved_config"]["sandbox"].endswith("profile-sandbox")
    assert data["backend_binding"]["profile_name"] == "profile"
    assert data["backend_binding"]["router"] == "ollama/profile"
    assert data["backend_binding"]["sandbox_root"].endswith("profile-sandbox")


def test_doctor_does_not_invoke_llm(monkeypatch):
    # If it tries to invoke LLM, MagicMock will raise TypeError or similar,
    # but we can explicitly assert our mocks aren't called
    import agent_os.graph

    mock_build = MagicMock()
    monkeypatch.setattr(agent_os.graph, "build_graph", mock_build)

    run_doctor(json_output=True)

    mock_build.assert_not_called()
