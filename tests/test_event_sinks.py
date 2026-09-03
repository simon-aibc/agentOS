from pathlib import Path

import pytest

from agent_os.webhooks import WebhookEventSink
from agent_os.workspace import WorkspaceLoadError, compose_workspace, load_workspace


class DummyEventSink:
    name = "dummy_sink"

    def emit(self, event: dict):
        pass


class SyntheticEntryPoint:
    def __init__(self, name: str, target: object = None, error: Exception | None = None):
        self.name = name
        self.target = target
        self.error = error

    def load(self):
        if self.error:
            raise self.error
        return self.target


def test_workspace_event_sink_plugin_discovery(tmp_path: Path, monkeypatch):
    eps = [SyntheticEntryPoint("custom_sink", target=DummyEventSink)]
    monkeypatch.setattr(
        "agent_os.plugins._get_entry_points",
        lambda group: eps if group == "agent_os.event_sinks" else [],
    )

    workspace_path = tmp_path / "workspace.toml"
    workspace_path.write_text(
        """
[workspace]
name = "sink-ws"

[backends]
architect = "codex"
executor = "codex"

[memory]
type = "markdown"
path = "."

[webhooks]
sinks = ["custom_sink"]
""",
        encoding="utf-8",
    )

    ws = load_workspace(workspace_path)
    composed = compose_workspace(ws)
    assert len(composed.event_sinks) == 1
    assert composed.event_sinks[0].name == "dummy_sink"


def test_workspace_event_sink_cannot_override_builtin(tmp_path: Path, monkeypatch):
    eps = [SyntheticEntryPoint("webhook", target=DummyEventSink)]
    monkeypatch.setattr(
        "agent_os.plugins._get_entry_points",
        lambda group: eps if group == "agent_os.event_sinks" else [],
    )

    workspace_path = tmp_path / "workspace.toml"
    workspace_path.write_text(
        """
[workspace]
name = "bad-sink-ws"

[backends]
architect = "codex"
executor = "codex"

[memory]
type = "markdown"
path = "."

[webhooks]
sinks = ["webhook"]
""",
        encoding="utf-8",
    )

    with pytest.raises(
        WorkspaceLoadError, match="cannot override built-in event sink 'webhook'"
    ):
        load_workspace(workspace_path)


def test_workspace_webhook_configuration_with_secret_env(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("MY_WEBHOOK_SECRET", "super-secret-key")
    monkeypatch.setattr(
        "agent_os.webhooks.socket.getaddrinfo",
        lambda *_args, **_kwargs: [(2, 1, 6, "", ("93.184.216.34", 443))],
    )

    workspace_path = tmp_path / "workspace.toml"
    workspace_path.write_text(
        """
[workspace]
name = "hook-ws"

[backends]
architect = "codex"
executor = "codex"

[memory]
type = "markdown"
path = "."

[webhooks]
url = "https://example.com/events"
secret_env = "MY_WEBHOOK_SECRET"
""",
        encoding="utf-8",
    )

    ws = load_workspace(workspace_path)
    composed = compose_workspace(ws)
    assert len(composed.event_sinks) == 1
    sink = composed.event_sinks[0]
    assert isinstance(sink, WebhookEventSink)
    assert sink.url == "https://example.com/events"
    assert sink.secret == "super-secret-key"


def test_workspace_webhook_missing_secret_env_safely_disables(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("UNSET_SECRET_ENV", raising=False)

    workspace_path = tmp_path / "workspace.toml"
    workspace_path.write_text(
        """
[workspace]
name = "disabled-hook-ws"

[backends]
architect = "codex"
executor = "codex"

[memory]
type = "markdown"
path = "."

[webhooks]
url = "https://example.com/events"
secret_env = "UNSET_SECRET_ENV"
""",
        encoding="utf-8",
    )

    ws = load_workspace(workspace_path)
    composed = compose_workspace(ws)
    # Disabled webhook -> 0 sinks, no error raised
    assert len(composed.event_sinks) == 0


def test_workspace_webhook_literal_secret_is_rejected(tmp_path: Path):
    workspace_path = tmp_path / "workspace.toml"
    workspace_path.write_text(
        """
[workspace]
name = "literal-secret-ws"

[backends]
architect = "codex"
executor = "codex"

[memory]
type = "markdown"
path = "."

[webhooks]
url = "https://example.com/events"
secret = "must-not-live-in-toml"
""",
        encoding="utf-8",
    )

    with pytest.raises(WorkspaceLoadError, match="literal secrets"):
        load_workspace(workspace_path)


def test_workspace_webhook_invalid_ssrf_url_fails_closed(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("MY_WEBHOOK_SECRET", "super-secret-key")

    workspace_path = tmp_path / "workspace.toml"
    workspace_path.write_text(
        """
[workspace]
name = "ssrf-ws"

[backends]
architect = "codex"
executor = "codex"

[memory]
type = "markdown"
path = "."

[webhooks]
url = "https://127.0.0.1:8080/events"
secret_env = "MY_WEBHOOK_SECRET"
""",
        encoding="utf-8",
    )

    with pytest.raises(WorkspaceLoadError, match="Blocked unsafe/internal IP"):
        load_workspace(workspace_path)


def test_workspace_webhook_insecure_http_rejected_by_default_and_allowed_with_flag(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setenv("MY_WEBHOOK_SECRET", "super-secret-key")
    monkeypatch.setattr(
        "agent_os.webhooks.socket.getaddrinfo",
        lambda *_args, **_kwargs: [(2, 1, 6, "", ("93.184.216.34", 80))],
    )

    # 1. Default rejects http://
    workspace_path = tmp_path / "workspace_default_http.toml"
    workspace_path.write_text(
        """
[workspace]
name = "insecure-http-ws"

[backends]
architect = "codex"
executor = "codex"

[memory]
type = "markdown"
path = "."

[webhooks]
url = "http://example.com/events"
secret_env = "MY_WEBHOOK_SECRET"
""",
        encoding="utf-8",
    )
    with pytest.raises(WorkspaceLoadError, match="Insecure HTTP webhook URLs are disallowed"):
        load_workspace(workspace_path)

    # 2. Explicit allow_insecure_http = true succeeds
    workspace_optin_path = tmp_path / "workspace_optin_http.toml"
    workspace_optin_path.write_text(
        """
[workspace]
name = "insecure-http-ws"

[backends]
architect = "codex"
executor = "codex"

[memory]
type = "markdown"
path = "."

[webhooks]
url = "http://example.com/events"
secret_env = "MY_WEBHOOK_SECRET"
allow_insecure_http = true
""",
        encoding="utf-8",
    )
    ws = load_workspace(workspace_optin_path)
    composed = compose_workspace(ws)
    assert len(composed.event_sinks) == 1
    assert composed.event_sinks[0].allow_insecure_http is True
