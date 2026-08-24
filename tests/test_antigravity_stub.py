from unittest.mock import MagicMock

import pytest

from agent_os.backends import (
    ANTIGRAVITY_NOT_SUPPORTED_MESSAGE,
    AntigravityAdapter,
    NotYetSupportedError,
    build_default_registry,
)
from agent_os.state import AgentState


def sample_state() -> AgentState:
    return {
        "messages": [],
        "task": "candidate backend",
        "plan": None,
        "executor_output": None,
        "human_feedback": None,
        "hot_context": None,
    }


def test_antigravity_is_registered_as_stub():
    registry = build_default_registry()
    adapters = dict(registry.items())

    assert adapters["antigravity"].stub is True
    assert adapters["claude-code"].stub is False
    assert adapters["codex"].stub is False


@pytest.mark.parametrize("role", ["architect", "executor"])
def test_antigravity_invoker_raises_on_call(role):
    invoker = AntigravityAdapter().build_invoker(role)

    with pytest.raises(NotYetSupportedError) as captured:
        invoker(sample_state())

    assert str(captured.value) == ANTIGRAVITY_NOT_SUPPORTED_MESSAGE


def test_antigravity_auth_status_never_probes_host(monkeypatch):
    subprocess_run = MagicMock()
    shutil_which = MagicMock()
    monkeypatch.setattr("subprocess.run", subprocess_run)
    monkeypatch.setattr("shutil.which", shutil_which)

    status = AntigravityAdapter().authentication_status()

    assert status.status == "unknown"
    assert "candidate stub" in status.detail
    subprocess_run.assert_not_called()
    shutil_which.assert_not_called()
