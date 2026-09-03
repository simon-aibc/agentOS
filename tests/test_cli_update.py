import argparse
import contextlib
import sqlite3
from unittest.mock import MagicMock, patch

from rich.console import Console

from agent_os.cli.app import build_parser
from agent_os.cli.update import (
    handle_update_command,
    is_docker_environment,
    run_pre_update_db_backups,
)
from agent_os.permission_store import PERMISSION_DB_ENV
from agent_os.update_check import UpdateInfo


def test_cli_update_parser_registration():
    parser = build_parser()
    args = parser.parse_args(["update", "--check"])
    assert args.command == "update"
    assert args.check is True
    assert args.yes is False


def test_docker_environment_detection(monkeypatch, tmp_path):
    assert not is_docker_environment()

    # Mock container environment
    monkeypatch.setenv("CONTAINER", "docker")
    assert is_docker_environment()


def test_handle_update_check_flag():
    console = Console(record=True)
    args = argparse.Namespace(
        check=True, force=False, yes=False, pull=False, reload=False
    )

    mock_info = UpdateInfo(
        current_version="2.2.0",
        latest_version="2.3.0",
        update_available=True,
        release_url="https://github.com/simon-aibc/agent-os-langgraph/releases/tag/v2.3.0",
    )

    with patch("agent_os.cli.update.check_for_update", return_value=mock_info):
        code = handle_update_command(args, console=console)
        assert code == 0
        text = console.export_text()
        assert "Current Version: 2.2.0" in text
        assert "Latest Version:  2.3.0" in text
        assert "Update available!" in text


def test_handle_update_pip_guidance():
    console = Console(record=True)
    args = argparse.Namespace(
        check=False, force=False, yes=False, pull=False, reload=False
    )

    mock_info = UpdateInfo(
        current_version="2.2.0",
        latest_version="2.3.0",
        update_available=True,
    )

    with (
        patch("agent_os.cli.update.check_for_update", return_value=mock_info),
        patch("agent_os.cli.update.is_docker_environment", return_value=False),
        patch(
            "agent_os.cli.update.run_pre_update_db_backups", return_value=["test.db"]
        ),
    ):
        code = handle_update_command(args, console=console)
        assert code == 0
        text = console.export_text()
        assert "pip install --upgrade agent-os-langgraph" in text
        assert "Verified/backed up database: test.db" in text


def test_handle_update_pip_execution_with_yes_flag():
    console = Console(record=True)
    args = argparse.Namespace(
        check=False, force=False, yes=True, pull=False, reload=False
    )

    mock_info = UpdateInfo(
        current_version="2.2.0",
        latest_version="2.3.0",
        update_available=True,
    )

    with (
        patch("agent_os.cli.update.check_for_update", return_value=mock_info),
        patch("agent_os.cli.update.is_docker_environment", return_value=False),
        patch("agent_os.cli.update.run_pre_update_db_backups", return_value=[]),
        patch("subprocess.run") as mock_run,
    ):
        mock_run.return_value = MagicMock(returncode=0)
        code = handle_update_command(args, console=console)
        assert code == 0
        mock_run.assert_called_once()
        text = console.export_text()
        assert "Successfully upgraded agent-os-langgraph!" in text


def test_handle_update_docker_guidance_and_pull_flag():
    console = Console(record=True)
    args = argparse.Namespace(
        check=False, force=False, yes=True, pull=True, reload=False
    )

    mock_info = UpdateInfo(
        current_version="2.2.0",
        latest_version="2.3.0",
        update_available=True,
    )

    with (
        patch("agent_os.cli.update.check_for_update", return_value=mock_info),
        patch("agent_os.cli.update.is_docker_environment", return_value=True),
        patch("agent_os.cli.update.run_pre_update_db_backups", return_value=[]),
        patch("subprocess.run") as mock_run,
    ):
        code = handle_update_command(args, console=console)
        assert code == 0
        mock_run.assert_called_once_with(["docker", "compose", "pull"], check=False)
        text = console.export_text()
        assert "Detected Docker container runtime." in text
        assert "Executing `docker compose pull`" in text


def test_handle_update_reload_flag():
    console = Console(record=True)
    args = argparse.Namespace(
        check=False, force=False, yes=False, pull=False, reload=True
    )

    mock_info = UpdateInfo(
        current_version="2.2.0",
        latest_version="2.3.0",
        update_available=True,
    )

    with (
        patch("agent_os.cli.update.check_for_update", return_value=mock_info),
        patch("agent_os.cli.update.is_docker_environment", return_value=False),
        patch("agent_os.cli.update.run_pre_update_db_backups", return_value=[]),
        patch("subprocess.run") as mock_run,
    ):
        code = handle_update_command(args, console=console)
        assert code == 0
        assert mock_run.call_count == 1
        args_called = mock_run.call_args[0][0]
        assert args_called[0] == "launchctl"
        assert args_called[1] == "kickstart"
        text = console.export_text()
        assert "Reloading daemon:" in text


def test_run_pre_update_db_backups_creates_real_backup_files(tmp_path, monkeypatch):
    test_db = tmp_path / "permissions.db"
    with contextlib.closing(sqlite3.connect(test_db)) as conn:
        conn.execute("CREATE TABLE test (id INT)")
        conn.execute("PRAGMA user_version = 2")
        conn.commit()

    monkeypatch.setenv(PERMISSION_DB_ENV, str(test_db))
    monkeypatch.setattr("agent_os.cli.update.get_runs_db_path", lambda: str(tmp_path / "nonexistent_runs.db"))
    monkeypatch.setattr("agent_os.cli.update.get_sched_db_path", lambda: str(tmp_path / "nonexistent_sched.db"))

    backed_up = run_pre_update_db_backups()
    assert str(test_db) in backed_up
    assert (tmp_path / "permissions.db.bak-2").exists()
