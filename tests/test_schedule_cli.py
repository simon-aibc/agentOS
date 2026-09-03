"""Tests for the schedule CLI subcommands (r1.8a.4)."""

from __future__ import annotations

import asyncio

import pytest

from agent_os.checkpoints import CHECKPOINT_DB_ENV
from agent_os.cli.app import async_main, build_parser
from agent_os.schedules import SCHED_DB_ENV


@pytest.fixture(autouse=True)
def _isolate_sched_db(tmp_path, monkeypatch):
    monkeypatch.setenv(CHECKPOINT_DB_ENV, str(tmp_path / "checkpoints.db"))
    monkeypatch.delenv(SCHED_DB_ENV, raising=False)


# ── Parser contract ─────────────────────────────────────────────────


def test_schedule_help_is_exposed():
    parser = build_parser()
    # Must not raise when parsing --help for the schedule command.
    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["schedule", "--help"])
    assert exc_info.value.code == 0


def test_schedule_add_parser():
    parser = build_parser()
    args = parser.parse_args(
        [
            "schedule",
            "add",
            "--name",
            "daily-run",
            "--kind",
            "run",
            "--cron",
            "0 9 * * *",
            "--task",
            "refactor",
        ]
    )
    assert args.command == "schedule"
    assert args.schedule_command == "add"
    assert args.name == "daily-run"
    assert args.kind == "run"
    assert args.cron == "0 9 * * *"
    assert args.task == "refactor"


def test_schedule_add_interval():
    parser = build_parser()
    args = parser.parse_args(
        [
            "schedule",
            "add",
            "--name",
            "hourly-brief",
            "--kind",
            "brief",
            "--every",
            "1h",
        ]
    )
    assert args.every == "1h"
    assert args.cron is None


# ── Functional integration ──────────────────────────────────────────


def test_schedule_add_list_remove(capsys):
    """Exercise the full add → list → remove lifecycle."""
    exit_code = asyncio.run(
        async_main(
            [
                "schedule",
                "add",
                "--name",
                "test-run",
                "--kind",
                "run",
                "--every",
                "30m",
                "--task",
                "test task",
            ]
        )
    )
    assert exit_code == 0
    capsys.readouterr()

    # List should show the schedule with FULL UUID.
    exit_code = asyncio.run(async_main(["schedule", "list"]))
    output = capsys.readouterr().out

    assert exit_code == 0
    # Find the UUID in the output
    lines = output.strip().split("\n")
    # Output looks like: [INFO] <uuid>  test-run  run ...
    assert len(lines) >= 1
    schedule_id = lines[0].split()[1]
    assert len(schedule_id) == 36  # Must be full UUID length

    # Now actually remove it
    exit_code = asyncio.run(
        async_main(
            [
                "schedule",
                "remove",
                schedule_id,
            ]
        )
    )
    assert exit_code == 0

    # List as JSON.
    exit_code = asyncio.run(async_main(["schedule", "list", "--json"]))
    assert exit_code == 0


def test_schedule_remove_unknown_returns_2():
    exit_code = asyncio.run(
        async_main(
            [
                "schedule",
                "remove",
                "nonexistent-id",
            ]
        )
    )
    assert exit_code == 2


def test_schedule_add_invalid_kind():
    """CLI must reject invalid kind/trigger combinations."""
    exit_code = asyncio.run(
        async_main(
            [
                "schedule",
                "add",
                "--name",
                "bad",
                "--kind",
                "run",
                # Missing --cron or --every.
            ]
        )
    )
    assert exit_code == 2


def test_schedule_add_run_requires_task():
    exit_code = asyncio.run(
        async_main(
            [
                "schedule",
                "add",
                "--name",
                "no-task",
                "--kind",
                "run",
                "--every",
                "1h",
                # Missing --task.
            ]
        )
    )
    assert exit_code == 2


def test_schedule_add_brief_rejects_task():
    exit_code = asyncio.run(
        async_main(
            [
                "schedule",
                "add",
                "--name",
                "brief-with-task",
                "--kind",
                "brief",
                "--every",
                "24h",
                "--task",
                "should fail",
            ]
        )
    )
    assert exit_code == 2


def test_normalize_argv_includes_schedule():
    """The schedule command must not be prepended with 'run'."""
    from agent_os.cli.app import _normalize_argv

    assert _normalize_argv(["schedule", "list"]) == ["schedule", "list"]


# ── app_callback CLI tests ──────────────────────────────────────────


def test_schedule_add_app_callback_parser():
    parser = build_parser()
    args = parser.parse_args(
        [
            "schedule",
            "add",
            "--name",
            "cb-sched",
            "--kind",
            "app_callback",
            "--every",
            "5m",
            "--url",
            "https://api.example.com/hook",
            "--method",
            "POST",
            "--headers",
            '{"X-Key": "val"}',
            "--body",
            '{"msg": "test"}',
        ]
    )
    assert args.kind == "app_callback"
    assert args.url == "https://api.example.com/hook"
    assert args.method == "POST"
    assert args.headers == '{"X-Key": "val"}'
    assert args.body == '{"msg": "test"}'


def test_schedule_add_app_callback_lifecycle(capsys):
    exit_code = asyncio.run(
        async_main(
            [
                "schedule",
                "add",
                "--name",
                "test-cb",
                "--kind",
                "app_callback",
                "--every",
                "15m",
                "--url",
                "https://api.example.com/hook",
                "--headers",
                '{"Authorization": "Bearer 123"}',
                "--body",
                '{"action": "ping"}',
            ]
        )
    )
    assert exit_code == 0
    capsys.readouterr()

    exit_code = asyncio.run(async_main(["schedule", "list"]))
    output = capsys.readouterr().out
    assert exit_code == 0
    assert "test-cb" in output
    assert "app_callback" in output


def test_schedule_run_once_app_callback(capsys):
    from unittest.mock import AsyncMock, MagicMock, patch

    exit_code = asyncio.run(
        async_main(
            [
                "schedule",
                "add",
                "--name",
                "run-once-cb",
                "--kind",
                "app_callback",
                "--every",
                "1h",
                "--url",
                "https://api.example.com/hook",
            ]
        )
    )
    assert exit_code == 0
    capsys.readouterr()

    # Get the schedule ID
    exit_code = asyncio.run(async_main(["schedule", "list"]))
    output = capsys.readouterr().out
    assert exit_code == 0
    schedule_id = output.strip().split("\n")[0].split()[1]

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.is_success = True

    mock_client = AsyncMock()
    mock_client.request.return_value = mock_resp
    mock_client.__aenter__.return_value = mock_client

    with patch("agent_os.scheduler.httpx.AsyncClient", return_value=mock_client):
        exit_code = asyncio.run(async_main(["schedule", "run-once", schedule_id]))
        assert exit_code == 0
        out = capsys.readouterr().out
        assert "Callback completed" in out


def test_schedule_add_app_callback_invalid_headers():
    exit_code = asyncio.run(
        async_main(
            [
                "schedule",
                "add",
                "--name",
                "bad-headers",
                "--kind",
                "app_callback",
                "--every",
                "1h",
                "--url",
                "https://example.com",
                "--headers",
                "not-json",
            ]
        )
    )
    assert exit_code == 2


def test_schedule_add_app_callback_invalid_body():
    exit_code = asyncio.run(
        async_main(
            [
                "schedule",
                "add",
                "--name",
                "bad-body",
                "--kind",
                "app_callback",
                "--every",
                "1h",
                "--url",
                "https://example.com",
                "--body",
                "not-json",
            ]
        )
    )
    assert exit_code == 2


def test_schedule_add_app_callback_missing_url():
    exit_code = asyncio.run(
        async_main(
            [
                "schedule",
                "add",
                "--name",
                "no-url",
                "--kind",
                "app_callback",
                "--every",
                "1h",
            ]
        )
    )
    assert exit_code == 2
