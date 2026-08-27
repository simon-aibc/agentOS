import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
from pydantic import BaseModel

from agent_os.cli_backends import (
    CliBackendAuthenticationError,
    CliBackendError,
    CliBackendTimeout,
    build_safe_subprocess_env,
    cli_timeout_seconds,
    ensure_binary,
    parse_claude_stream_json,
    parse_codex_output_file,
    prepare_codex_home,
    run_cli_command,
    write_schema_file,
)


class DummySchema(BaseModel):
    foo: str
    bar: int


def test_missing_binary():
    with patch("shutil.which", return_value=None):
        with pytest.raises(ValueError, match="Binary 'missing_bin' not found on PATH"):
            ensure_binary("missing_bin")


def test_successful_binary_detection():
    with patch("shutil.which", return_value="/fake/path/bin"):
        path = ensure_binary("bin")
        assert path == "/fake/path/bin"


def test_sandbox_cwd_enforcement(monkeypatch, tmp_path):
    sandbox = tmp_path / "sandbox"
    monkeypatch.setenv("AGENT_OS_SANDBOX", str(sandbox))

    with (
        patch("shutil.which", return_value="/fake/path/fake_bin"),
        patch("subprocess.run") as mock_run,
    ):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )
        run_cli_command("fake_bin", ["arg"])

    mock_run.assert_called_once()
    command_args = mock_run.call_args.args[0]
    kwargs = mock_run.call_args.kwargs
    assert command_args == ["/fake/path/fake_bin", "arg"]
    assert kwargs["cwd"] == sandbox.resolve()
    assert kwargs["stdin"] == subprocess.DEVNULL
    assert kwargs["shell"] is False


def test_sandbox_path_escape_rejected(monkeypatch, tmp_path):
    sandbox = tmp_path / "sandbox"
    monkeypatch.setenv("AGENT_OS_SANDBOX", str(sandbox))

    with pytest.raises(CliBackendError, match="expand sandbox access"):
        run_cli_command("codex", ["exec", "--cd", "/etc", "task"])


@pytest.mark.parametrize(
    "args, expected_error",
    [
        (["exec", "--sandbox", "danger-full-access", "task"], "danger-full-access"),
        (["-p", "task", "--permission-mode=bypassPermissions"], "bypassPermissions"),
        (["-p", "task", "--add-dir=/var/tmp"], "expand sandbox access"),
        (
            ["exec", "--dangerously-bypass-approvals-and-sandbox", "task"],
            "expand sandbox access",
        ),
    ],
)
def test_cli_security_bypass_arguments_rejected(args, expected_error):
    with pytest.raises(CliBackendError, match=expected_error):
        run_cli_command("fake_bin", args)


def test_env_strips_credentials(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "secret_key")
    monkeypatch.setenv("MY_TOKEN", "secret_token")
    monkeypatch.setenv("SUPER_SECRET", "123")
    monkeypatch.setenv("MY_PASSWORD", "pwd")
    monkeypatch.setenv("AWS_CREDENTIAL_PROFILE", "prof")
    monkeypatch.setenv("AUTH_BEARER", "jwt")
    monkeypatch.setenv("NORMAL_ENV", "ok")
    monkeypatch.setenv("NOT_A_SECRET", "public")

    safe_env = build_safe_subprocess_env()

    assert "OPENAI_API_KEY" not in safe_env
    assert "MY_TOKEN" not in safe_env
    assert "SUPER_SECRET" not in safe_env
    assert "MY_PASSWORD" not in safe_env
    assert "AWS_CREDENTIAL_PROFILE" not in safe_env
    assert "AUTH_BEARER" not in safe_env

    assert "NORMAL_ENV" in safe_env
    assert safe_env["NORMAL_ENV"] == "ok"


def test_prepare_codex_home_isolates_cache_and_links_existing_auth(tmp_path):
    user_home = tmp_path / "user"
    source_home = user_home / ".codex"
    source_home.mkdir(parents=True)
    source_auth = source_home / "auth.json"
    source_auth.write_text('{"auth": "test"}', encoding="utf-8")
    runtime_home = tmp_path / "agent-os-codex"

    prepared = prepare_codex_home({
        "HOME": str(user_home),
        "AGENT_OS_CODEX_HOME": str(runtime_home),
    })

    assert prepared["CODEX_HOME"] == str(runtime_home)
    assert (runtime_home / "auth.json").is_symlink()
    assert (runtime_home / "auth.json").resolve() == source_auth


def test_prepare_codex_home_respects_explicit_shared_home(tmp_path):
    user_home = tmp_path / "user"
    source_home = user_home / ".codex"
    source_home.mkdir(parents=True)
    env = {"HOME": str(user_home), "AGENT_OS_CODEX_HOME": str(source_home)}

    assert prepare_codex_home(env) == env


def test_codex_command_receives_isolated_codex_home(monkeypatch, tmp_path):
    sandbox = tmp_path / "sandbox"
    user_home = tmp_path / "user"
    source_home = user_home / ".codex"
    source_home.mkdir(parents=True)
    (source_home / "auth.json").write_text('{"auth": "test"}', encoding="utf-8")
    runtime_home = tmp_path / "agent-os-codex"
    monkeypatch.setenv("AGENT_OS_SANDBOX", str(sandbox))
    monkeypatch.setenv("HOME", str(user_home))
    monkeypatch.setenv("AGENT_OS_CODEX_HOME", str(runtime_home))

    with (
        patch("shutil.which", return_value="/fake/path/codex"),
        patch("subprocess.run") as mock_run,
    ):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )
        run_cli_command("codex", ["exec", "task"])

    assert mock_run.call_args.kwargs["env"]["CODEX_HOME"] == str(runtime_home)


def test_cli_timeout_uses_a_bounded_agent_os_configuration(monkeypatch):
    monkeypatch.setenv("AGENT_OS_CLI_TIMEOUT_SECONDS", "900")
    assert cli_timeout_seconds() == 900
    monkeypatch.setenv("AGENT_OS_CLI_TIMEOUT_SECONDS", "invalid")
    assert cli_timeout_seconds() == 300
    assert cli_timeout_seconds(1) == 1
    assert cli_timeout_seconds(9999) == 9999


def test_schema_file_cleanup_success():
    schema_path_str = None
    with write_schema_file(DummySchema) as path:
        schema_path_str = str(path)
        assert path.exists()

        # Verify it contains the json schema
        content = json.loads(path.read_text(encoding="utf-8"))
        assert content["title"] == "DummySchema"
        assert "foo" in content["properties"]

    # File should be removed after context exit
    assert not Path(schema_path_str).exists()


def test_schema_file_cleanup_on_exception():
    schema_path_str = None
    try:
        with write_schema_file(DummySchema) as path:
            schema_path_str = str(path)
            assert path.exists()
            raise RuntimeError("Fake error")
    except RuntimeError:
        pass

    assert not Path(schema_path_str).exists()


def test_timeout_handling(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_OS_SANDBOX", str(tmp_path))

    with (
        patch("shutil.which", return_value="/fake/path/fake_bin"),
        patch("subprocess.run") as mock_run,
    ):
        mock_run.side_effect = subprocess.TimeoutExpired(
            cmd="fake_bin",
            timeout=1.5,
            stderr=b"some output",
        )

        with pytest.raises(CliBackendTimeout) as exc_info:
            run_cli_command("fake_bin", ["arg"], timeout_seconds=1)

        assert "timed out after 1 seconds" in str(exc_info.value)
        assert "some output" in str(exc_info.value)


def test_nonzero_subprocess_handling(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_OS_SANDBOX", str(tmp_path))
    monkeypatch.setenv("MY_API_KEY", "super_secret_string")

    with (
        patch("shutil.which", return_value="/fake/path/fake_bin"),
        patch("subprocess.run") as mock_run,
    ):
        mock_run.return_value = subprocess.CompletedProcess(
            args=["fake_bin"],
            returncode=1,
            stdout="",
            stderr="Error involving api_key=super_secret_string inside!",
        )

        with pytest.raises(CliBackendError) as exc_info:
            run_cli_command("fake_bin", ["arg"])

        err_str = str(exc_info.value)
        assert "failed with return code 1" in err_str
        assert "super_secret_string" not in err_str
        assert "[REDACTED]" in err_str


def test_authentication_subprocess_handling(tmp_path):
    with (
        patch("shutil.which", return_value="/fake/path/claude"),
        patch("subprocess.run") as mock_run,
    ):
        mock_run.return_value = subprocess.CompletedProcess(
            args=["claude"],
            returncode=1,
            stdout="",
            stderr="authentication_failed: please login again",
        )

        with pytest.raises(CliBackendAuthenticationError) as exc_info:
            run_cli_command("claude", ["arg"])

        err_str = str(exc_info.value)
        assert "failed due to authentication" in err_str
        assert "Run `claude auth login`" in err_str
        assert "authentication_failed" in err_str


def test_codex_authentication_guidance_redacts_secret(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENAI_API_KEY", "secret-value")
    monkeypatch.setenv("AGENT_OS_SANDBOX", str(tmp_path))

    with (
        patch("shutil.which", return_value="/fake/path/codex"),
        patch("subprocess.run") as mock_run,
    ):
        mock_run.return_value = subprocess.CompletedProcess(
            args=["codex"],
            returncode=1,
            stdout="",
            stderr="invalid_api_key=secret-value",
        )

        with pytest.raises(CliBackendAuthenticationError) as exc_info:
            run_cli_command("codex", ["exec", "task"])

    error = str(exc_info.value)
    assert "Run `codex login`" in error
    assert "secret-value" not in error
    assert "[REDACTED]" in error


def test_codex_output_parser_success(tmp_path):
    out_file = tmp_path / "out.json"
    out_file.write_text('{"success": true, "data": "test"}')

    data = parse_codex_output_file(out_file)
    assert data == {"success": True, "data": "test"}


def test_codex_output_parser_invalid_missing(monkeypatch, tmp_path):
    missing_file = tmp_path / "missing.json"
    with pytest.raises(ValueError, match="Codex output file not found"):
        parse_codex_output_file(missing_file)

    monkeypatch.setenv("TEST_API_KEY", "secret-value")
    invalid_file = tmp_path / "invalid.json"
    invalid_file.write_text("not json api_key=secret-value", encoding="utf-8")
    with pytest.raises(
        ValueError, match="Failed to parse Codex JSON output"
    ) as exc_info:
        parse_codex_output_file(invalid_file)
    assert "secret-value" not in str(exc_info.value)


def test_codex_output_parser_rejects_non_object(tmp_path):
    out_file = tmp_path / "out.json"
    out_file.write_text('["not", "an", "object"]', encoding="utf-8")

    with pytest.raises(ValueError, match="Expected Codex output to be a JSON object"):
        parse_codex_output_file(out_file)


def test_claude_stream_json_parser_success_direct():
    stdout = '{"some": "garbage"}\n{"final": "payload"}'
    data = parse_claude_stream_json(stdout)
    assert data == {"final": "payload"}


def test_claude_stream_json_parser_success_assistant():
    stdout = json.dumps(
        {
            "type": "assistant",
            "message": {
                "content": [{"type": "text", "text": '{"structured": "data"}'}]
            },
        }
    )
    data = parse_claude_stream_json(stdout)
    assert data == {"structured": "data"}


def test_claude_stream_json_parser_success_markdown_fence():
    stdout = json.dumps(
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": '```json\n{"structured": "data"}\n```'}
                ]
            },
        }
    )
    data = parse_claude_stream_json(stdout)
    assert data == {"structured": "data"}


def test_claude_stream_json_parser_success_result():
    stdout = json.dumps({"type": "result", "result": '{"structured": "data"}'})
    data = parse_claude_stream_json(stdout)
    assert data == {"structured": "data"}


def test_claude_stream_json_parser_success_structured_output():
    stdout = json.dumps({"type": "result", "structured_output": {"schema": "valid"}})
    data = parse_claude_stream_json(stdout)
    assert data == {"schema": "valid"}


def test_claude_stream_json_parser_invalid():
    stdout = 'just plain text\nmore text\n{"invalid: json'
    with pytest.raises(ValueError, match="No valid JSON payload found"):
        parse_claude_stream_json(stdout)
