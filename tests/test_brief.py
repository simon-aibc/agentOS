import datetime

import pytest

from agent_os.brief import generate_brief, write_brief
from agent_os.connectors import MarkdownVaultConnector
from agent_os.hot_context import load_hot_context
from agent_os.memory_gate import evaluate_write_policy


def test_generate_brief_structure(tmp_path):
    connector = MarkdownVaultConnector(tmp_path)
    (tmp_path / "AI" / "Logs").mkdir(parents=True)

    # Yesterday is 2026-08-06
    with open(tmp_path / "AI" / "Logs" / "2026-08-06.md", "w") as f:
        f.write("Did some coding yesterday.")

    with open(tmp_path / "goals.md", "w") as f:
        f.write("Focus on testing today.")

    sessions = [
        {"thread_id": "thread-123", "title": "Test session", "updated_at": "2026-08-06"}
    ]

    def mock_summarizer(prompt: str) -> str:
        assert "Did some coding yesterday." in prompt
        assert "Focus on testing today." in prompt
        assert "Test session" in prompt
        return "1. Hôm qua: Coding\n2. Focus hôm nay: Testing\n3. Threads đang chờ: Test session"

    brief = generate_brief(
        connector,
        sessions,
        date="2026-08-07",
        summarizer=mock_summarizer,
        spine_sources=["goals.md"],
    )
    assert "Hôm qua: Coding" in brief
    assert "Focus hôm nay: Testing" in brief
    assert "Threads đang chờ: Test session" in brief


def test_generate_brief_empty(tmp_path):
    connector = MarkdownVaultConnector(tmp_path)

    def mock_summarizer(prompt: str) -> str:
        return "Empty brief"

    # Empty vault, empty sessions
    brief = generate_brief(
        connector,
        [],
        date="2026-08-07",
        summarizer=mock_summarizer,
        spine_sources=["goals.md"],
    )
    assert brief == "Empty brief"


def test_brief_written_to_briefs_auto(tmp_path):
    connector = MarkdownVaultConnector(tmp_path)
    (tmp_path / "AI" / "Briefs").mkdir(parents=True, exist_ok=True)

    res = write_brief(connector, "My brief content", "2026-08-07")

    # Check that it auto-approved and wrote
    assert res.committed is True
    assert res.ref == "AI/Briefs/2026-08-07.md"

    # Check provenance
    content = (tmp_path / "AI" / "Briefs" / "2026-08-07.md").read_text()
    assert "via: morning-brief" in content
    assert "My brief content" in content


def test_policy_briefs_auto():
    assert evaluate_write_policy("AI/Briefs/x.md", "append") == "auto"
    assert evaluate_write_policy("AI/Briefs/x.md", "create") == "auto"
    assert evaluate_write_policy("AI/Briefs/x.md", "overwrite") == "gate"


def test_context_spine_order(tmp_path):
    connector = MarkdownVaultConnector(tmp_path)

    # Create files
    (tmp_path / "system.md").write_text("sys")
    (tmp_path / "invariants.md").write_text("inv")
    (tmp_path / "goals.md").write_text("goal")

    content = load_hot_context(
        connector, sources=["system.md", "invariants.md", "goals.md"]
    )

    idx_sys = content.find("sys")
    idx_inv = content.find("inv")
    idx_goal = content.find("goal")

    assert idx_sys != -1
    assert idx_inv != -1
    assert idx_goal != -1
    assert idx_sys < idx_inv < idx_goal


@pytest.mark.integration
def test_real_llm_smoke(tmp_path):
    import os

    if (
        "OPENAI_API_KEY" not in os.environ
        and "ANTHROPIC_API_KEY" not in os.environ
        and "LLM_ARCHITECT" not in os.environ
    ):
        pytest.skip("No real LLM configured for integration test")

    connector = MarkdownVaultConnector(tmp_path)
    (tmp_path / "AI" / "Logs").mkdir(parents=True, exist_ok=True)

    yesterday = (datetime.datetime.now() - datetime.timedelta(days=1)).strftime(
        "%Y-%m-%d"
    )
    with open(tmp_path / "AI" / "Logs" / f"{yesterday}.md", "w") as f:
        f.write("I fixed a bug in the routing logic and wrote 3 tests.")

    with open(tmp_path / "goals.md", "w") as f:
        f.write("Ship v1.6 today.")

    sessions = [
        {"thread_id": "thr1", "title": "Debug routing", "updated_at": yesterday}
    ]

    # Real summarizer via the CLI architect invoker (same proven path as the
    # r1.5c summarize smoke), not the removed `agent_os.backends.BackendBinding`.
    def invoke_summarizer(prompt: str) -> str:
        from agent_os.agents.cli_architect import build_cli_architect_invoker

        invoker = build_cli_architect_invoker("claude-code")
        return invoker({"task": prompt, "messages": []}).summary

    brief = generate_brief(
        connector,
        sessions,
        date=datetime.datetime.now().strftime("%Y-%m-%d"),
        summarizer=invoke_summarizer,
        spine_sources=["goals.md"],
    )

    assert "routing" in brief.lower() or "bug" in brief.lower()
    assert "v1.6" in brief.lower()


def test_resolve_brief_connector_prefers_workspace(monkeypatch):
    from unittest.mock import MagicMock

    from agent_os.brief_runtime import resolve_brief_connector

    mock_runtime = MagicMock()
    mock_connector = MagicMock()
    mock_runtime.memory_connector = mock_connector

    monkeypatch.setattr(
        "agent_os.server.runtime.composed_workspace",
        lambda: mock_runtime,
    )

    # When no explicit name is passed, returns workspace connector
    assert resolve_brief_connector() is mock_connector
