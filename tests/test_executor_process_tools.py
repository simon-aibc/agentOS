import re
import sys
from unittest.mock import patch

import pytest

from agent_os.schemas import BashResult
from agent_os.tools.bash import bash
from agent_os.tools.run_tests import run_tests


@pytest.fixture
def test_sandbox(tmp_path, monkeypatch):
    sandbox_dir = tmp_path / "sandbox"
    sandbox_dir.mkdir()
    monkeypatch.setenv("AGENT_OS_SANDBOX", str(sandbox_dir))
    return sandbox_dir


def test_bash_cwd_is_sandbox(test_sandbox):
    # Print working directory
    result = bash.invoke({"cmd_args": ["pwd"]})
    assert isinstance(result, BashResult)
    # The output of pwd should contain the sandbox path (or match it exactly)
    assert str(test_sandbox.resolve()) in result.stdout


def test_bash_creates_missing_sandbox(tmp_path, monkeypatch):
    sandbox_dir = tmp_path / "missing" / "sandbox"
    monkeypatch.setenv("AGENT_OS_SANDBOX", str(sandbox_dir))

    result = bash.invoke({"cmd_args": ["pwd"]})

    assert result.returncode == 0
    assert sandbox_dir.is_dir()
    assert str(sandbox_dir.resolve()) in result.stdout


def test_bash_output_captured(test_sandbox):
    # Run a simple echo command
    result = bash.invoke({"cmd_args": ["echo", "hello world"]})
    assert result.returncode == 0
    assert "hello world" in result.stdout
    assert result.stderr == ""


def test_bash_output_truncation(test_sandbox):
    stdout_bytes = 110 * 1024
    stderr_bytes = 120 * 1024
    script = (
        f"import sys; sys.stdout.write('A' * {stdout_bytes}); "
        f"sys.stderr.write('B' * {stderr_bytes})"
    )

    result = bash.invoke({"cmd_args": [sys.executable, "-c", script]})

    assert result.returncode == 0
    assert result.truncated is True
    for original_size, output in (
        (stdout_bytes, result.stdout),
        (stderr_bytes, result.stderr),
    ):
        match = re.match(r"^\[truncated (\d+) bytes\]\n", output)
        assert match is not None
        retained = output[match.end() :]
        assert len(output.encode()) <= 100 * 1024
        assert int(match.group(1)) == original_size - len(retained.encode())


def test_bash_timeout_works(test_sandbox):
    # Sleep for longer than the timeout
    result = bash.invoke({"cmd_args": ["sleep", "2"], "timeout_seconds": 1})
    assert result.timed_out is True
    assert result.returncode is None


def test_bash_missing_executable(test_sandbox):
    result = bash.invoke({"cmd_args": ["non_existent_command_123"]})
    assert result.returncode == -1
    assert "No such file or directory" in result.stderr or "not found" in result.stderr


def test_bash_shell_metacharacters_not_interpreted(test_sandbox):
    # A shell would expand $HOME; argv execution prints it literally.
    result = bash.invoke({"cmd_args": ["echo", "$HOME"]})
    assert result.returncode == 0
    assert "$HOME" in result.stdout


@patch("agent_os.tools.bash.subprocess.run")
def test_bash_subprocess_receives_shell_false(mock_run, test_sandbox):
    # We patch subprocess.run to verify the kwargs
    mock_run.return_value.returncode = 0
    mock_run.return_value.stdout = "ok"
    mock_run.return_value.stderr = ""

    bash.invoke({"cmd_args": ["echo", "test"]})

    mock_run.assert_called_once()
    kwargs = mock_run.call_args.kwargs
    assert kwargs["shell"] is False
    assert kwargs["cwd"] == test_sandbox.resolve()


def test_run_tests_invokes_pytest(test_sandbox):
    # Create a dummy test file in the sandbox
    test_file = test_sandbox / "test_dummy.py"
    test_file.write_text("def test_ok(): pass", encoding="utf-8")

    result = run_tests.invoke({"pytest_args": [str(test_file)]})

    # Assert pytest actually ran and passed
    assert result.returncode == 0
    assert "1 passed" in result.stdout
    assert result.args == [sys.executable, "-m", "pytest", str(test_file)]


@patch("agent_os.tools.run_tests.bash")
def test_run_tests_forwards_timeout(mock_bash, test_sandbox):
    mock_bash.invoke.return_value = BashResult(
        args=[sys.executable, "-m", "pytest"],
        returncode=0,
        stdout="",
        stderr="",
        timed_out=False,
    )

    run_tests.invoke({"pytest_args": [], "timeout_seconds": 45})

    mock_bash.invoke.assert_called_once_with(
        {
            "cmd_args": [sys.executable, "-m", "pytest"],
            "timeout_seconds": 45,
        }
    )


@patch("agent_os.tools.run_tests.bash")
def test_run_tests_defaults_to_120_seconds(mock_bash, test_sandbox):
    mock_bash.invoke.return_value = BashResult(
        args=[sys.executable, "-m", "pytest"],
        returncode=0,
        stdout="",
        stderr="",
        timed_out=False,
    )

    run_tests.invoke({"pytest_args": []})

    assert mock_bash.invoke.call_args.args[0]["timeout_seconds"] == 120
