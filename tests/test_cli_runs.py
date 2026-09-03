import io
import json

import pytest
from rich.console import Console

from agent_os.checkpoints import CHECKPOINT_DB_ENV
from agent_os.cli.app import async_main
from agent_os.runs import create_run, record_approval


@pytest.fixture
def test_env(tmp_path, monkeypatch):
    db_path = str(tmp_path / "checkpoints.sqlite")
    monkeypatch.setenv(CHECKPOINT_DB_ENV, db_path)
    return tmp_path


@pytest.mark.anyio
async def test_cli_runs_approvals_not_found(test_env):
    output = io.StringIO()
    console = Console(file=output, force_terminal=False, color_system=None)

    code = await async_main(["runs", "approvals", "nonexistent-id"], console=console)
    assert code == 1
    assert "not found" in output.getvalue()


@pytest.mark.anyio
async def test_cli_runs_approvals_empty(test_env):
    run_id = create_run("thread-cli-1", None, "test task")
    output = io.StringIO()
    console = Console(file=output, force_terminal=False, color_system=None)

    code = await async_main(["runs", "approvals", run_id], console=console)
    assert code == 0
    assert "No approval decisions recorded" in output.getvalue()


@pytest.mark.anyio
async def test_cli_runs_approvals_human_and_json(test_env, capsys):
    run_id = create_run("thread-cli-2", "/workspace", "test task")
    record_approval(
        run_id,
        decision="approved",
        actor_id="local:operator",
        actor_kind="human",
        reason="Approved manually",
    )
    record_approval(
        run_id,
        decision="cancelled",
        actor_id="token:ci-service",
        actor_kind="service",
        on_behalf_of="lead-dev",
    )

    # Human-readable output
    output = io.StringIO()
    console = Console(file=output, force_terminal=False, color_system=None)
    code = await async_main(["runs", "approvals", run_id], console=console)
    assert code == 0
    rendered = output.getvalue()
    assert "#1 [approved] actor=local:operator (human)" in rendered
    assert "reason=Approved manually" in rendered
    assert "#2 [cancelled] actor=token:ci-service (service)" in rendered
    assert "on_behalf_of=lead-dev" in rendered

    # JSON output
    code_json = await async_main(["runs", "approvals", run_id, "--json"])
    assert code_json == 0
    captured = capsys.readouterr()
    parsed = json.loads(captured.out)
    assert len(parsed) == 2
    assert parsed[0]["seq"] == 1
    assert parsed[0]["decision"] == "approved"
    assert parsed[1]["seq"] == 2
    assert parsed[1]["decision"] == "cancelled"
