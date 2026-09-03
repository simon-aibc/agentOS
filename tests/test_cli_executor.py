import json
import os
import shutil
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agent_os.agents.cli_executor import (
    build_cli_executor_invoker,
    build_executor_prompt,
)
from agent_os.cli_backends import CliBackendError
from agent_os.schemas import CodingPlan, CodingResult


def test_build_executor_prompt():
    plan = CodingPlan(
        summary="summary",
        files=["test.py"],
        changes=["Add a test"],
        verify_cmd="pytest test.py",
    )
    prompt = build_executor_prompt(plan)

    assert "apply the following approved architectural plan" in prompt.casefold()
    assert (
        "Do NOT access or modify any files outside the current working directory"
        in prompt
    )
    assert "pytest test.py" in prompt
    assert "test.py" in prompt
    assert "Add a test" in prompt
    assert "CodingResult" in prompt


def test_build_cli_executor_invoker_unknown_backend():
    with pytest.raises(ValueError, match="Unsupported CLI executor backend: unknown"):
        build_cli_executor_invoker("unknown")


def test_cli_executor_invoker_missing_plan():
    invoker = build_cli_executor_invoker("claude-code")
    state = {"plan": None}
    with pytest.raises(
        ValueError, match="State 'plan' must be a PlanArtifact instance."
    ):
        invoker(state)


def test_build_cli_executor_invoker_claude_success(monkeypatch, tmp_path):
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    monkeypatch.setenv("AGENT_OS_SANDBOX", str(sandbox))

    invoker = build_cli_executor_invoker("claude-code")
    plan = CodingPlan(summary="s", files=["f1"], changes=["c1"], verify_cmd="v1")
    state = {"plan": plan}

    with patch("agent_os.backends.run_cli_command") as mock_run:
        mock_run.return_value = MagicMock(
            stdout='{"type": "result", "structured_output": {"status": "completed", "diff": "d", "verify_output": "vo"}}'
        )
        report = invoker(state)

        assert isinstance(report, CodingResult)
        assert report.success is True
        assert report.status == "completed"
        assert report.diff == "d"
        assert report.verify_output == "vo"

        # Verify safe args
        assert mock_run.call_args.args[0] == "claude"
        args = mock_run.call_args.args[1]
        assert "--permission-mode" in args
        assert args[args.index("--permission-mode") + 1] == "acceptEdits"
        assert "--verbose" in args
        assert "--add-dir" not in args
        assert "--dangerously-skip-permissions" not in args
        schema = json.loads(args[args.index("--json-schema") + 1])
        assert schema["title"] == "CodingResult"
        assert "additionalProperties" not in schema


def test_build_cli_executor_invoker_claude_invalid_payload(monkeypatch, tmp_path):
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    monkeypatch.setenv("AGENT_OS_SANDBOX", str(sandbox))

    invoker = build_cli_executor_invoker("claude-code")
    plan = CodingPlan(summary="s", files=["f1"], changes=["c1"], verify_cmd="v1")
    state = {"plan": plan}

    with patch("agent_os.backends.run_cli_command") as mock_run:
        # Invalid enum value for status will trigger validation error
        mock_run.return_value = MagicMock(
            stdout='{"type": "result", "structured_output": {"status": "invalid_status"}}'
        )

        with pytest.raises(ValueError) as exc_info:
            invoker(state)

        assert (
            "Failed to validate claude-code structured output as CodingResult"
            in str(exc_info.value)
        )
        assert "structured_output" not in str(exc_info.value).lower()


def test_build_cli_executor_invoker_claude_invalid_json_is_redacted(
    monkeypatch, tmp_path
):
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    monkeypatch.setenv("AGENT_OS_SANDBOX", str(sandbox))
    monkeypatch.setenv("TEST_API_KEY", "secret-value")
    invoker = build_cli_executor_invoker("claude-code")
    plan = CodingPlan(summary="s", files=["f1"], changes=["c1"], verify_cmd="v1")

    with patch("agent_os.backends.run_cli_command") as mock_run:
        mock_run.return_value = MagicMock(stdout="invalid api_key=secret-value")
        with pytest.raises(ValueError, match="No valid JSON payload") as exc_info:
            invoker({"plan": plan})

    assert "secret-value" not in str(exc_info.value)


def test_cli_executor_does_not_retry_transient_failure(monkeypatch, tmp_path):
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    monkeypatch.setenv("AGENT_OS_SANDBOX", str(sandbox))
    invoker = build_cli_executor_invoker("claude-code")
    plan = CodingPlan(summary="s", files=["f1"], changes=["c1"], verify_cmd="v1")

    with patch(
        "agent_os.backends.run_cli_command",
        side_effect=CliBackendError("503 service unavailable"),
    ) as mock_run:
        with pytest.raises(CliBackendError, match="503"):
            invoker({"plan": plan})

    mock_run.assert_called_once()


def test_build_cli_executor_invoker_codex_success(monkeypatch, tmp_path):
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    monkeypatch.setenv("AGENT_OS_SANDBOX", str(sandbox))

    invoker = build_cli_executor_invoker("codex")
    plan = CodingPlan(summary="s", files=["f1"], changes=["c1"], verify_cmd="v1")
    state = {"plan": plan}

    schema_file = None
    output_file = None

    def mock_run_command(binary, args):
        nonlocal schema_file, output_file
        assert binary == "codex"

        # Codex must receive --sandbox workspace-write
        assert "--sandbox" in args
        assert args[args.index("--sandbox") + 1] == "workspace-write"
        assert "read-only" not in args
        assert "danger-full-access" not in args
        assert "--dangerously-bypass-approvals-and-sandbox" not in args
        assert "--add-dir" not in args

        idx_schema = args.index("--output-schema")
        schema_file = args[idx_schema + 1]
        assert os.path.exists(schema_file)
        schema = json.loads(Path(schema_file).read_text(encoding="utf-8"))
        assert schema["additionalProperties"] is False
        assert schema["required"] == list(schema["properties"])
        assert "$defs" not in schema
        assert "outputs" not in schema["properties"]
        assert "usage" not in schema["properties"]

        idx_output = args.index("--output-last-message")
        output_file = args[idx_output + 1]

        with open(output_file, "w", encoding="utf-8") as f:
            json.dump({"status": "completed", "diff": "d2", "verify_output": "vo2"}, f)

        return MagicMock()

    with patch("agent_os.backends.run_cli_command", side_effect=mock_run_command):
        report = invoker(state)

        assert isinstance(report, CodingResult)
        assert report.success is True
        assert report.status == "completed"
        assert report.diff == "d2"
        assert report.verify_output == "vo2"

    # Temporary files should be removed
    assert schema_file is not None and not os.path.exists(schema_file)
    assert output_file is not None and not os.path.exists(output_file)


def test_build_cli_executor_invoker_codex_invalid_payload(monkeypatch, tmp_path):
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    monkeypatch.setenv("AGENT_OS_SANDBOX", str(sandbox))

    invoker = build_cli_executor_invoker("codex")
    plan = CodingPlan(summary="s", files=["f1"], changes=["c1"], verify_cmd="v1")
    state = {"plan": plan}

    def mock_run_command(binary, args):
        idx_output = args.index("--output-last-message")
        output_file = args[idx_output + 1]
        with open(output_file, "w", encoding="utf-8") as f:
            json.dump({"status": "invalid_status"}, f)  # Invalid enum value for status

        return MagicMock()

    with patch("agent_os.backends.run_cli_command", side_effect=mock_run_command):
        with pytest.raises(ValueError) as exc_info:
            invoker(state)

        assert "Failed to validate codex structured output as CodingResult" in str(
            exc_info.value
        )


def test_build_cli_executor_invoker_codex_temp_cleanup_on_exception(
    monkeypatch, tmp_path
):
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    monkeypatch.setenv("AGENT_OS_SANDBOX", str(sandbox))
    invoker = build_cli_executor_invoker("codex")
    plan = CodingPlan(summary="s", files=["f1"], changes=["c1"], verify_cmd="v1")
    schema_file = None
    output_file = None

    def mock_run_command(binary, args):
        nonlocal schema_file, output_file
        schema_file = args[args.index("--output-schema") + 1]
        output_file = args[args.index("--output-last-message") + 1]
        raise CliBackendError("503 service unavailable")

    with patch(
        "agent_os.backends.run_cli_command",
        side_effect=mock_run_command,
    ) as mock_run:
        with pytest.raises(CliBackendError, match="503"):
            invoker({"plan": plan})

    mock_run.assert_called_once()
    assert schema_file is not None and not os.path.exists(schema_file)
    assert output_file is not None and not os.path.exists(output_file)


@pytest.mark.integration
def test_integration_cli_executor_claude(monkeypatch, tmp_path):
    if not shutil.which("claude"):
        pytest.skip("claude binary not found on PATH")

    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    monkeypatch.setenv("AGENT_OS_SANDBOX", str(sandbox))

    sentinel = sandbox / "sentinel.txt"
    original_content = "DO NOT EDIT ME"
    sentinel.write_text(original_content, encoding="utf-8")

    invoker = build_cli_executor_invoker("claude-code")
    plan = CodingPlan(
        summary="test",
        files=["sentinel.txt"],
        changes=["Change 'DO NOT EDIT ME' to 'I WAS HERE'"],
        verify_cmd="cat sentinel.txt | grep 'I WAS HERE'",
    )
    state = {"plan": plan}

    report = invoker(state)

    assert isinstance(report, CodingResult)
    assert report.success is True
    assert report.verify_output
    assert sentinel.read_text(encoding="utf-8") == "I WAS HERE"


@pytest.mark.integration
def test_integration_cli_executor_codex(monkeypatch, tmp_path):
    if not shutil.which("codex"):
        pytest.skip("codex binary not found on PATH")

    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    monkeypatch.setenv("AGENT_OS_SANDBOX", str(sandbox))

    sentinel = sandbox / "sentinel.txt"
    original_content = "DO NOT EDIT ME"
    sentinel.write_text(original_content, encoding="utf-8")

    invoker = build_cli_executor_invoker("codex")
    plan = CodingPlan(
        summary="test",
        files=["sentinel.txt"],
        changes=["Change 'DO NOT EDIT ME' to 'I WAS HERE'"],
        verify_cmd="cat sentinel.txt | grep 'I WAS HERE'",
    )
    state = {"plan": plan}

    report = invoker(state)

    assert isinstance(report, CodingResult)
    assert report.success is True
    assert report.verify_output
    assert sentinel.read_text(encoding="utf-8") == "I WAS HERE"
