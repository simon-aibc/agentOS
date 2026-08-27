import copy
import json
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from agent_os.sandbox import get_sandbox_root

_FORBIDDEN_CLI_ARGS = {
    "-C",
    "--add-dir",
    "--allow-dangerously-skip-permissions",
    "--cd",
    "--cwd",
    "--dangerously-bypass-approvals-and-sandbox",
    "--dangerously-skip-permissions",
    "--workdir",
    "--working-directory",
}
_FORBIDDEN_CLI_ARG_PREFIXES = tuple(
    f"{option}=" for option in _FORBIDDEN_CLI_ARGS if option.startswith("--")
)
DEFAULT_CLI_TIMEOUT_SECONDS = 300
MIN_CLI_TIMEOUT_SECONDS = 30
MAX_CLI_TIMEOUT_SECONDS = 1800


class CliBackendError(Exception):
    """Base exception for CLI execution failures."""


class CliBackendAuthenticationError(CliBackendError):
    """Raised when a CLI backend fails due to authentication issues."""


class CliBackendTimeout(CliBackendError):
    """Exception raised when a CLI backend command times out."""


def ensure_binary(binary_name: str) -> str:
    """Ensure a binary is available on the PATH."""
    path = shutil.which(binary_name)
    if path is None:
        raise ValueError(
            f"Binary '{binary_name}' not found on PATH. "
            f"Please ensure it is installed and authenticated."
        )
    return path


def _is_sensitive_key(key: str) -> bool:
    """Check if an environment variable key is sensitive."""
    key_upper = key.upper()
    sensitive_substrings = [
        "API_KEY",
        "TOKEN",
        "SECRET",
        "PASSWORD",
        "CREDENTIAL",
        "AUTH",
    ]
    return any(sub in key_upper for sub in sensitive_substrings)


def _coerce_text(value: object) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value or "")


def _redact_secrets_in_text(text: str) -> str:
    """
    Best-effort redaction of secret-looking values in text.
    Replaces values of sensitive environment variables if they are present in the text.
    """
    if not text:
        return text

    # We iterate through the current environment to find sensitive values
    # and replace them in the text.
    redacted_text = text
    for key, value in os.environ.items():
        if _is_sensitive_key(key) and value and len(value) > 3:
            redacted_text = redacted_text.replace(value, "[REDACTED]")

    redacted_text = re.sub(
        r"(?i)(api[_-]?key|token|secret|password|credential|auth)"
        r"(\s*[=:]\s*)([^\s'\";,]+)",
        r"\1\2[REDACTED]",
        redacted_text,
    )
    return redacted_text


def build_safe_subprocess_env() -> dict[str, str]:
    """Build a sanitized environment dictionary for subprocesses."""
    safe_env = {}
    for key, value in os.environ.items():
        if not _is_sensitive_key(key):
            safe_env[key] = value
    return safe_env


def prepare_codex_home(env: dict[str, str]) -> dict[str, str]:
    """Give Agent OS Codex invocations an isolated, authenticated CLI home.

    The desktop app and the standalone Codex CLI can update their model caches
    independently. Sharing ``~/.codex`` caused an incompatible cache schema to
    stall Agent OS executor calls. Keep the CLI cache/config state separate,
    while linking only the existing authentication file so credentials are not
    copied into the Agent OS runtime directory.
    """
    prepared = dict(env)
    user_home = Path(prepared.get("HOME") or Path.home()).expanduser()
    source_home = user_home / ".codex"
    runtime_home = Path(
        prepared.get("AGENT_OS_CODEX_HOME") or user_home / ".agent-os" / "codex"
    ).expanduser()

    if runtime_home.resolve() == source_home.resolve():
        # An operator explicitly chose the shared home. Preserve that intent.
        return prepared

    runtime_home.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        runtime_home.chmod(0o700)
    except OSError:
        # The directory is still usable; a later CLI auth error will be clearer
        # than failing an otherwise safe invocation because chmod was rejected.
        pass

    source_auth = source_home / "auth.json"
    runtime_auth = runtime_home / "auth.json"
    if source_auth.is_file() and not runtime_auth.exists() and not runtime_auth.is_symlink():
        runtime_auth.symlink_to(source_auth)

    prepared["CODEX_HOME"] = str(runtime_home)
    return prepared


def cli_timeout_seconds(value: int | None = None) -> int:
    """Return a bounded CLI timeout, optionally configured for Agent OS."""
    if value is not None:
        # Explicit call-site timeouts are part of the public helper contract
        # (and are useful for fast tests); only deployment configuration is
        # bounded defensively.
        return int(value)

    raw_value: object = os.getenv(
        "AGENT_OS_CLI_TIMEOUT_SECONDS", DEFAULT_CLI_TIMEOUT_SECONDS
    )
    try:
        timeout = int(raw_value)
    except (TypeError, ValueError):
        timeout = DEFAULT_CLI_TIMEOUT_SECONDS
    return min(max(timeout, MIN_CLI_TIMEOUT_SECONDS), MAX_CLI_TIMEOUT_SECONDS)


def _validate_cli_args(args: list[str]) -> None:
    """Reject arguments that can escape or disable the configured sandbox."""
    for index, arg in enumerate(args):
        if arg in _FORBIDDEN_CLI_ARGS or arg.startswith(_FORBIDDEN_CLI_ARG_PREFIXES):
            raise CliBackendError(
                f"CLI argument '{arg}' is forbidden because it can expand sandbox access"
            )

        next_arg = args[index + 1] if index + 1 < len(args) else None
        if arg in {"-s", "--sandbox"} and next_arg == "danger-full-access":
            raise CliBackendError("CLI sandbox mode 'danger-full-access' is forbidden")
        if arg == "--permission-mode" and next_arg == "bypassPermissions":
            raise CliBackendError(
                "CLI permission mode 'bypassPermissions' is forbidden"
            )
        if arg == "--sandbox=danger-full-access":
            raise CliBackendError("CLI sandbox mode 'danger-full-access' is forbidden")
        if arg == "--permission-mode=bypassPermissions":
            raise CliBackendError(
                "CLI permission mode 'bypassPermissions' is forbidden"
            )


def strict_json_schema(schema_model: type[BaseModel]) -> dict[str, Any]:
    """
    Generate a strict JSON schema where every object node requires all its
    properties and forbids additional properties. This is required by the
    OpenAI structured output backend (used by Codex).
    """
    schema_dict = copy.deepcopy(schema_model.model_json_schema())

    def visit(node: Any) -> None:
        if isinstance(node, list):
            for item in node:
                visit(item)
        elif isinstance(node, dict):
            for value in node.values():
                visit(value)

            properties = node.get("properties")
            is_object = node.get("type") == "object" or isinstance(properties, dict)
            if is_object:
                node["additionalProperties"] = False
                node["required"] = (
                    list(properties) if isinstance(properties, dict) else []
                )

    visit(schema_dict)
    return schema_dict


@contextmanager
def write_schema_file(
    schema_model: type[BaseModel], *, strict: bool = False
) -> Iterator[Path]:
    """Context manager to write a Pydantic schema to a temporary JSON file."""
    if strict:
        schema_dict = strict_json_schema(schema_model)
    else:
        schema_dict = schema_model.model_json_schema()

    # We create the temp file in the system temp directory, not the repo root
    fd, temp_path_str = tempfile.mkstemp(suffix=".json", text=True)
    temp_path = Path(temp_path_str)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(schema_dict, f, indent=2)
        yield temp_path
    finally:
        if temp_path.exists():
            temp_path.unlink()


def run_cli_command(
    binary: str,
    args: list[str],
    timeout_seconds: int | None = None,
) -> subprocess.CompletedProcess[str]:
    """
    Run a CLI command in the sandbox with a sanitized environment.
    Raises clear errors on timeout or non-zero exit.
    """
    _validate_cli_args(args)
    effective_timeout_seconds = cli_timeout_seconds(timeout_seconds)
    binary_path = ensure_binary(binary)
    sandbox_root = get_sandbox_root()
    sandbox_root.mkdir(parents=True, exist_ok=True)

    safe_env = build_safe_subprocess_env()
    if binary == "codex":
        safe_env = prepare_codex_home(safe_env)
    cmd = [binary_path] + args

    try:
        result = subprocess.run(
            cmd,
            cwd=sandbox_root,
            stdin=subprocess.DEVNULL,
            shell=False,
            capture_output=True,
            text=True,
            timeout=effective_timeout_seconds,
            env=safe_env,
        )
    except subprocess.TimeoutExpired as e:
        stderr_excerpt = _redact_secrets_in_text(_coerce_text(e.stderr)[-500:])
        raise CliBackendTimeout(
            f"Command '{binary}' timed out after {effective_timeout_seconds} seconds. "
            f"Stderr excerpt: {stderr_excerpt}"
        ) from e

    if result.returncode != 0:
        stderr_text = _coerce_text(result.stderr)
        stderr_excerpt = _redact_secrets_in_text(stderr_text[-1000:])

        # Check for authentication errors
        auth_indicators = [
            "authentication_failed",
            "oauth session expired",
            "invalid_api_key",
            "not logged in",
            "authentication required",
            "unauthorized",
        ]

        lower_stderr = stderr_text.lower()
        if any(indicator in lower_stderr for indicator in auth_indicators):
            if binary == "claude":
                guidance = "Run `claude auth login` before starting agent-os."
            elif binary == "codex":
                guidance = "Run `codex login` before starting agent-os."
            else:
                guidance = f"Authenticate {binary} before starting agent-os."

            raise CliBackendAuthenticationError(
                f"Command '{binary}' failed due to authentication. "
                f"{guidance} Subprocesses are noninteractive and cannot complete login. "
                f"Stderr excerpt: {stderr_excerpt}"
            )

        raise CliBackendError(
            f"Command '{binary}' failed with return code {result.returncode}. "
            f"Stderr excerpt: {stderr_excerpt}"
        )

    return result


def parse_codex_output_file(path: Path) -> dict[str, Any]:
    """Parse JSON from a Codex output file."""
    if not path.exists():
        raise ValueError(f"Codex output file not found: {path}")

    try:
        content = path.read_text(encoding="utf-8")
        data = json.loads(content)
    except json.JSONDecodeError as e:
        excerpt = _redact_secrets_in_text(content[:200])
        raise ValueError(
            f"Failed to parse Codex JSON output: {e}. Excerpt: {excerpt}"
        ) from e
    except Exception as e:
        raise ValueError(f"Failed to read Codex output file: {e}") from e

    if not isinstance(data, dict):
        raise ValueError(
            f"Expected Codex output to be a JSON object, got {type(data).__name__}"
        )

    return data


def _parse_json_text_payload(text: str) -> dict[str, Any] | None:
    text = text.strip()
    if not text:
        return None
    match = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
    if match:
        text = match.group(1).strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _extract_claude_event_payload(event: dict[str, Any]) -> dict[str, Any] | None:
    event_type = event.get("type")
    if event_type == "result":
        structured_output = event.get("structured_output")
        if isinstance(structured_output, dict):
            return structured_output

        result_content = event.get("result")
        if isinstance(result_content, dict):
            return result_content
        if isinstance(result_content, str):
            return _parse_json_text_payload(result_content)
        return None

    if event_type in {"assistant", "message"}:
        message = event.get("message")
        if not isinstance(message, dict):
            return None
        content = message.get("content")
        if not isinstance(content, list):
            return None
        for block in reversed(content):
            if not isinstance(block, dict):
                continue
            if block.get("type") != "text":
                continue
            parsed = _parse_json_text_payload(str(block.get("text", "")))
            if parsed is not None:
                return parsed
        return None

    if event_type is None:
        return event

    return None


def parse_claude_stream_json(stdout: str) -> dict[str, Any]:
    """
    Parse a Claude Code stream-json output to extract the final structured message.
    """
    if not stdout:
        raise ValueError("Claude CLI provided no stdout")

    parsed_events: list[dict[str, Any]] = []
    lines = stdout.strip().splitlines()

    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
            if isinstance(parsed, dict):
                parsed_events.append(parsed)
        except json.JSONDecodeError:
            continue

    for event in reversed(parsed_events):
        payload = _extract_claude_event_payload(event)
        if payload is not None:
            return payload

    excerpt = _redact_secrets_in_text(stdout[-500:])
    raise ValueError(
        f"No valid JSON payload found in Claude output. Excerpt: {excerpt}"
    )
