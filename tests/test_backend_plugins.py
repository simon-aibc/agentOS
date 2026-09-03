import pytest

from agent_os.backends import AuthStatus, build_default_registry


class DummyBackendAdapter:
    name = "custom_llm"
    binary_name = "custom_llm"
    supported_roles = frozenset({"architect", "executor"})
    stub = False

    def build_invoker(self, role):
        return lambda state: None

    def authentication_status(self):
        return AuthStatus(status="ok")


class SyntheticEntryPoint:
    def __init__(self, name: str, target: object = None, error: Exception | None = None):
        self.name = name
        self.target = target
        self.error = error

    def load(self):
        if self.error:
            raise self.error
        return self.target


def test_backend_plugin_discovery_and_resolution(monkeypatch):
    eps = [SyntheticEntryPoint("custom_llm", target=DummyBackendAdapter)]
    monkeypatch.setattr(
        "agent_os.plugins._get_entry_points",
        lambda group: eps if group == "agent_os.backends" else [],
    )

    registry = build_default_registry()
    assert "custom_llm" in registry.names
    assert "codex" in registry.names
    assert "claude-code" in registry.names

    adapter = registry.resolve("executor", "custom_llm")
    assert adapter.name == "custom_llm"


def test_backend_plugin_cannot_override_builtin(monkeypatch):
    eps = [SyntheticEntryPoint("codex", target=DummyBackendAdapter)]
    monkeypatch.setattr(
        "agent_os.plugins._get_entry_points",
        lambda group: eps if group == "agent_os.backends" else [],
    )

    with pytest.raises(ValueError, match="Plugin in 'agent_os.backends' cannot override built-in backend 'codex'"):
        build_default_registry()


def test_backend_plugin_cannot_override_hyphenated_builtin(monkeypatch):
    eps = [SyntheticEntryPoint("claude-code", target=DummyBackendAdapter)]
    monkeypatch.setattr(
        "agent_os.plugins._get_entry_points",
        lambda group: eps if group == "agent_os.backends" else [],
    )

    with pytest.raises(
        ValueError,
        match="Plugin in 'agent_os.backends' cannot override built-in backend 'claude-code'",
    ):
        build_default_registry()


def test_backend_plugin_unsupported_factory_args_fails_closed(monkeypatch):
    def bad_backend_factory(unsupported_arg: str):
        return DummyBackendAdapter()

    eps = [SyntheticEntryPoint("bad_backend", target=bad_backend_factory)]
    monkeypatch.setattr(
        "agent_os.plugins._get_entry_points",
        lambda group: eps if group == "agent_os.backends" else [],
    )

    with pytest.raises(ValueError, match="requires unsupported arguments"):
        build_default_registry()
