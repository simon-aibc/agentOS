from collections.abc import Callable

import pytest

from agent_os.backends import (
    AuthStatus,
    BackendAdapter,
    BackendRegistry,
)
from agent_os.schemas import ArchitectBrief
from agent_os.state import AgentState


class FakeAdapter:
    name = "fake"
    binary_name = "fake-cli"
    supported_roles = frozenset({"architect", "executor"})

    def build_invoker(self, role: str) -> Callable[[AgentState], object]:
        return lambda state: state

    def authentication_status(self) -> AuthStatus:
        return AuthStatus(status="unknown", detail="test adapter")


class ArchitectOnlyAdapter:
    name = "architect-only"
    binary_name = "architect-cli"
    supported_roles = frozenset({"architect"})

    def build_invoker(self, role: str) -> Callable[[AgentState], object]:
        return lambda state: ArchitectBrief(files=[], changes=[], verify_cmd="")

    def authentication_status(self) -> AuthStatus:
        return AuthStatus(status="unknown")


def test_register_and_resolve_returns_same_adapter() -> None:
    registry = BackendRegistry()
    adapter = FakeAdapter()

    registry.register(adapter)

    assert registry.names == ("fake",)
    assert registry.resolve("architect", "fake") is adapter
    assert registry.resolve("executor", "fake") is adapter


def test_register_rejects_name_collision() -> None:
    registry = BackendRegistry()
    registry.register(FakeAdapter())

    with pytest.raises(ValueError, match="already registered: fake"):
        registry.register(FakeAdapter())


def test_resolve_unknown_name_lists_registered_adapters() -> None:
    registry = BackendRegistry()
    registry.register(FakeAdapter())

    with pytest.raises(ValueError, match="Unknown backend adapter 'missing'.*fake"):
        registry.resolve("architect", "missing")


def test_resolve_rejects_unsupported_role() -> None:
    registry = BackendRegistry()
    registry.register(ArchitectOnlyAdapter())

    with pytest.raises(ValueError, match="does not support role 'executor'"):
        registry.resolve("executor", "architect-only")


def test_protocol_shape_and_auth_status() -> None:
    adapter: BackendAdapter = FakeAdapter()
    assert adapter.binary_name == "fake-cli"
    assert adapter.supported_roles == frozenset({"architect", "executor"})
    assert adapter.authentication_status().status == "unknown"


def test_registry_items_stable_sort() -> None:
    registry = BackendRegistry()
    adapter1 = FakeAdapter()
    adapter2 = ArchitectOnlyAdapter()

    registry.register(adapter1)
    registry.register(adapter2)

    items = registry.items()
    assert isinstance(items, tuple)
    assert len(items) == 2

    # "architect-only" sorts before "fake"
    assert items[0][0] == "architect-only"
    assert items[0][1] is adapter2
    assert items[1][0] == "fake"
    assert items[1][1] is adapter1

    # Test stability
    assert registry.items() == items


def test_claude_adapter_auth_missing_binary(monkeypatch) -> None:
    from agent_os.backends import ClaudeCodeAdapter

    monkeypatch.setattr("shutil.which", lambda _: None)
    adapter = ClaudeCodeAdapter()
    status = adapter.authentication_status()
    assert status.status == "unknown"
    assert "not found on PATH" in status.detail


def test_claude_adapter_auth_success(monkeypatch) -> None:
    import subprocess

    from agent_os.backends import ClaudeCodeAdapter

    monkeypatch.setattr("shutil.which", lambda _: "/fake/claude")

    def fake_run(*args, **kwargs):
        assert kwargs["stdin"] is subprocess.DEVNULL
        assert kwargs["shell"] is False
        assert kwargs["timeout"] == 5.0
        assert "TEST_API_KEY" not in kwargs["env"]
        return subprocess.CompletedProcess(
            args=args, returncode=0, stdout="You are logged in.", stderr=""
        )

    monkeypatch.setenv("TEST_API_KEY", "secret-value")
    monkeypatch.setattr(subprocess, "run", fake_run)

    adapter = ClaudeCodeAdapter()
    status = adapter.authentication_status()
    assert status.status == "ok"


def test_claude_adapter_auth_json_unauthenticated(monkeypatch) -> None:
    import subprocess

    from agent_os.backends import ClaudeCodeAdapter

    monkeypatch.setattr("shutil.which", lambda _: "/fake/claude")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args=args,
            returncode=0,
            stdout='{ "loggedIn": false, "authMethod": "none" }',
            stderr="",
        ),
    )

    status = ClaudeCodeAdapter().authentication_status()
    assert status.status == "unauthenticated"


def test_claude_adapter_auth_unauthenticated(monkeypatch) -> None:
    import subprocess

    from agent_os.backends import ClaudeCodeAdapter

    monkeypatch.setattr("shutil.which", lambda _: "/fake/claude")

    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(
            args=args, returncode=1, stdout="", stderr="authentication_failed"
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    adapter = ClaudeCodeAdapter()
    status = adapter.authentication_status()
    assert status.status == "unauthenticated"
    assert "claude auth login" in status.detail


def test_codex_adapter_auth_missing_binary(monkeypatch) -> None:
    from agent_os.backends import CodexAdapter

    monkeypatch.setattr("shutil.which", lambda _: None)
    adapter = CodexAdapter()
    status = adapter.authentication_status()
    assert status.status == "unknown"
    assert "not found on PATH" in status.detail


def test_codex_adapter_auth_success(monkeypatch) -> None:
    import subprocess

    from agent_os.backends import CodexAdapter

    monkeypatch.setattr("shutil.which", lambda _: "/fake/codex")

    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(
            args=args, returncode=0, stdout="Authenticated as user", stderr=""
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    adapter = CodexAdapter()
    status = adapter.authentication_status()
    assert status.status == "ok"


def test_codex_adapter_auth_unauthenticated(monkeypatch) -> None:
    import subprocess

    from agent_os.backends import CodexAdapter

    monkeypatch.setattr("shutil.which", lambda _: "/fake/codex")

    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(
            args=args, returncode=1, stdout="", stderr="oauth session expired"
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    adapter = CodexAdapter()
    status = adapter.authentication_status()
    assert status.status == "unauthenticated"
    assert "codex login" in status.detail
