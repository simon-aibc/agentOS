import os
from unittest.mock import MagicMock

import pytest

from agent_os.cli.app import main


@pytest.fixture
def mock_profiles(monkeypatch):
    from agent_os.profiles import Profile, ProfileFile

    pf = ProfileFile(
        default="def",
        profiles={
            "def": Profile(name="def", sandbox="./def-sandbox"),
            "env": Profile(name="env", sandbox="./env-sandbox"),
            "cli": Profile(name="cli", sandbox="./cli-sandbox"),
            "bad": Profile(name="bad", router="cli/not-allowed"),
        },
    )
    monkeypatch.setattr("agent_os.profiles.load_profiles", lambda *a, **k: pf)
    # mock registry so resolution succeeds for backends if any
    from agent_os.backends import BackendRegistry

    class DummyAdapter:
        name = "dummy"
        binary_name = "dummy"
        supported_roles = frozenset({"architect", "executor"})

    reg = BackendRegistry()
    reg.register(DummyAdapter())
    monkeypatch.setattr("agent_os.backends.build_default_registry", lambda: reg)
    return pf


@pytest.fixture
def mock_graph_runner(monkeypatch):
    import agent_os.cli.app

    runner = MagicMock(return_value=0)

    # Use AsyncMock for _run_graph since it's an async function
    async def async_runner(*args, **kwargs):
        return runner(*args, **kwargs)

    monkeypatch.setattr(agent_os.cli.app, "_run_graph", async_runner)

    # We must mock graph_factory in main to avoid full checkpoint load
    import agent_os.graph

    monkeypatch.setattr(agent_os.graph, "build_graph", lambda checkpointer: MagicMock())
    return runner


def test_cli_precedence_flag(monkeypatch, mock_profiles, mock_graph_runner, tmp_path):
    monkeypatch.setenv("AGENT_OS_PROFILE", "env")
    monkeypatch.setenv("AGENT_OS_SANDBOX", "/original")

    res = main(["run", "task", "--profile", "cli"])
    assert res == 0
    assert os.environ["AGENT_OS_SANDBOX"] == "/original"  # Restored


def test_cli_sandbox_override(monkeypatch, mock_profiles, mock_graph_runner, tmp_path):
    monkeypatch.setenv("AGENT_OS_SANDBOX", "/original")

    # Sandbox flag should override profile sandbox during run
    res = main(["run", "task", "--profile", "cli", "--sandbox", "/flag-sandbox"])
    assert res == 0
    assert os.environ["AGENT_OS_SANDBOX"] == "/original"


def test_cli_invalid_profile(monkeypatch, mock_profiles, mock_graph_runner, capsys):
    res = main(["run", "task", "--profile", "missing"])
    assert res == 2
    out = capsys.readouterr().out
    assert "Profile error" in out
    mock_graph_runner.assert_not_called()


def test_cli_profile_router_error(
    monkeypatch, mock_profiles, mock_graph_runner, capsys
):
    res = main(["run", "task", "--profile", "bad"])
    assert res == 2
    out = capsys.readouterr().out
    assert "Profile error" in out
    mock_graph_runner.assert_not_called()


def test_doctor_profile_env(monkeypatch, mock_profiles, capsys):
    monkeypatch.setenv("AGENT_OS_PROFILE", "env")
    res = main(["doctor"])
    assert res == 0
    out = capsys.readouterr().out
    assert "Profile: env (via env)" in out


def test_doctor_profile_default(monkeypatch, mock_profiles, capsys):
    monkeypatch.delenv("AGENT_OS_PROFILE", raising=False)
    res = main(["doctor"])
    assert res == 0
    out = capsys.readouterr().out
    assert "Profile: def (via default)" in out


def test_doctor_invalid_profile(monkeypatch, mock_profiles, capsys):
    monkeypatch.setenv("AGENT_OS_PROFILE", "missing")
    res = main(["doctor"])
    assert res == 1
    out = capsys.readouterr().out
    assert "Profile error" in out
    assert "STATUS: FAIL" in out
