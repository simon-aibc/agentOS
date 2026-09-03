"""Tests for the shared brief execution seam (r1.8a.2)."""

from __future__ import annotations

import datetime as dt
from unittest.mock import MagicMock, patch

from agent_os.brief_runtime import (
    BriefExecutionResult,
    build_brief_summarizer,
    execute_brief,
    resolve_brief_connector,
)

# ── resolve_brief_connector ─────────────────────────────────────────


def test_resolve_brief_connector_default_markdown(monkeypatch, tmp_path):
    monkeypatch.delenv("AGENT_OS_MEMORY_CONNECTOR", raising=False)
    monkeypatch.setenv("AGENT_OS_VAULT_PATH", str(tmp_path))
    connector = resolve_brief_connector()
    from agent_os.connectors import MarkdownVaultConnector

    assert isinstance(connector, MarkdownVaultConnector)


def test_resolve_brief_connector_explicit_markdown(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_OS_MEMORY_CONNECTOR", "markdown")
    monkeypatch.setenv("AGENT_OS_VAULT_PATH", str(tmp_path))
    connector = resolve_brief_connector(name="markdown")
    from agent_os.connectors import MarkdownVaultConnector

    assert isinstance(connector, MarkdownVaultConnector)


def test_resolve_brief_connector_gbrain(monkeypatch):
    monkeypatch.setenv("AGENT_OS_MEMORY_CONNECTOR", "gbrain")
    connector = resolve_brief_connector(name="gbrain")
    from agent_os.connectors import GbrainConnector

    assert isinstance(connector, GbrainConnector)


# ── build_brief_summarizer ──────────────────────────────────────────


def test_build_brief_summarizer_cli_model(monkeypatch):
    monkeypatch.setenv("LLM_ARCHITECT", "cli/claude-code")
    with patch("agent_os.brief_runtime.build_cli_architect_invoker") as mock_build:
        mock_invoker = MagicMock()
        mock_invoker.return_value = MagicMock(summary="test brief")
        mock_build.return_value = mock_invoker

        summarizer = build_brief_summarizer()
        result = summarizer("test prompt")

        mock_build.assert_called_once_with("claude-code")
        assert result == "test brief"


def test_build_brief_summarizer_api_model(monkeypatch):
    monkeypatch.setenv("LLM_ARCHITECT", "gpt-4o")
    with patch("agent_os.brief_runtime.get_architect_llm") as mock_get:
        mock_llm = MagicMock()
        mock_llm.invoke.return_value = MagicMock(content="api brief")
        mock_get.return_value = mock_llm

        summarizer = build_brief_summarizer(model="gpt-4o")
        result = summarizer("test prompt")

        mock_get.assert_called_once_with("gpt-4o")
        assert result == "api brief"


def test_build_brief_summarizer_api(monkeypatch):
    monkeypatch.setenv("LLM_ARCHITECT", "openai/gpt-4")

    with patch("agent_os.brief_runtime.get_architect_llm") as mock_get:
        mock_llm = MagicMock()
        mock_llm.invoke.return_value = MagicMock(content="api summarized")
        mock_get.return_value = mock_llm

        summarizer = build_brief_summarizer()
        result = summarizer("api prompt")

        assert result == "api summarized"
        mock_get.assert_called_once_with("openai/gpt-4")


def test_build_brief_summarizer_empty_fallback(monkeypatch):
    # If the user sets LLM_ARCHITECT to an empty string, it should fallback to cli/claude-code
    monkeypatch.setenv("LLM_ARCHITECT", "")

    with patch("agent_os.brief_runtime.build_cli_architect_invoker") as mock_build:
        mock_invoker = MagicMock()
        mock_invoker.return_value = MagicMock(summary="empty fallback summarized")
        mock_build.return_value = mock_invoker

        summarizer = build_brief_summarizer()
        result = summarizer("test prompt")

        assert result == "empty fallback summarized"
        mock_build.assert_called_once_with("claude-code")


# ── execute_brief ───────────────────────────────────────────────────


def test_execute_brief_write_true(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_OS_VAULT_PATH", str(tmp_path))
    monkeypatch.delenv("AGENT_OS_MEMORY_CONNECTOR", raising=False)

    with (
        patch("agent_os.brief_runtime.generate_brief") as mock_gen,
        patch("agent_os.brief_runtime.write_brief") as mock_write,
        patch("agent_os.brief_runtime.list_sessions") as mock_sessions,
        patch("agent_os.brief_runtime.build_brief_summarizer") as mock_summarizer,
    ):
        mock_sessions.return_value = []
        mock_gen.return_value = "# Brief content"
        mock_write.return_value = MagicMock(ref="AI/Briefs/2026-01-01.md")
        mock_summarizer.return_value = lambda prompt: "summarized"

        result = execute_brief(date="2026-01-01", write=True)

        assert isinstance(result, BriefExecutionResult)
        assert result.date == "2026-01-01"
        assert result.content == "# Brief content"
        assert result.ref == "AI/Briefs/2026-01-01.md"
        mock_write.assert_called_once()


def test_execute_brief_write_false(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_OS_VAULT_PATH", str(tmp_path))
    monkeypatch.delenv("AGENT_OS_MEMORY_CONNECTOR", raising=False)

    with (
        patch("agent_os.brief_runtime.generate_brief") as mock_gen,
        patch("agent_os.brief_runtime.write_brief") as mock_write,
        patch("agent_os.brief_runtime.list_sessions") as mock_sessions,
        patch("agent_os.brief_runtime.build_brief_summarizer") as mock_summarizer,
    ):
        mock_sessions.return_value = []
        mock_gen.return_value = "# Brief content"
        mock_summarizer.return_value = lambda prompt: "summarized"

        result = execute_brief(date="2026-01-01", write=False)

        assert result.content == "# Brief content"
        assert result.ref is None
        mock_write.assert_not_called()


def test_execute_brief_date_defaults_to_today(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_OS_VAULT_PATH", str(tmp_path))
    monkeypatch.delenv("AGENT_OS_MEMORY_CONNECTOR", raising=False)

    today = dt.datetime.now(dt.UTC).strftime("%Y-%m-%d")

    with (
        patch("agent_os.brief_runtime.generate_brief") as mock_gen,
        patch("agent_os.brief_runtime.write_brief"),
        patch("agent_os.brief_runtime.list_sessions") as mock_sessions,
        patch("agent_os.brief_runtime.build_brief_summarizer") as mock_summarizer,
    ):
        mock_sessions.return_value = []
        mock_gen.return_value = "# Brief"
        mock_summarizer.return_value = lambda prompt: "summarized"

        result = execute_brief(write=False)

        assert result.date == today
