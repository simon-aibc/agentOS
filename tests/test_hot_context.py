import os
from datetime import UTC, datetime

from agent_os.connectors import MarkdownVaultConnector
from agent_os.hot_context import load_hot_context


def test_load_hot_context_basic(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "hot.md").write_text("hot content")

    ai_mem = vault / "AI" / "Memory"
    ai_mem.mkdir(parents=True)
    (ai_mem / "a.md").write_text("a content")

    connector = MarkdownVaultConnector(str(vault))
    res = load_hot_context(connector)
    assert "hot content" in res
    assert "a content" in res
    assert res.index("hot content") < res.index("a content")


def test_load_hot_context_max_chars(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "hot.md").write_text("H" * 5000)

    ai_mem = vault / "AI" / "Memory"
    ai_mem.mkdir(parents=True)
    (ai_mem / "a.md").write_text("A" * 5000)

    connector = MarkdownVaultConnector(str(vault))
    res = load_hot_context(connector, max_chars=8000)

    # We should have H, and part of A, but max_chars exactly
    # len of f"--- BEGIN ... ---\n{content}\n--- END ... ---\n"
    # total len must be <= 8000
    assert len(res) <= 8000
    assert "H" * 5000 in res
    assert "A" * 5000 not in res
    assert "A" * 1000 in res


def test_load_hot_context_max_age(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    hot_file = vault / "hot.md"
    hot_file.write_text("hot")

    ai_mem = vault / "AI" / "Memory"
    ai_mem.mkdir(parents=True)
    old_file = ai_mem / "old.md"
    old_file.write_text("old")

    past = datetime.now(UTC).timestamp() - (20 * 24 * 3600)
    os.utime(old_file, (past, past))

    connector = MarkdownVaultConnector(str(vault))
    res = load_hot_context(connector, max_age_days=14)

    assert "hot" in res
    assert "old" not in res


def test_load_hot_context_empty(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    connector = MarkdownVaultConnector(str(vault))
    res = load_hot_context(connector)
    assert res == ""


def test_architect_node_prefers_workspace_memory_connector(monkeypatch):
    from unittest.mock import MagicMock

    from agent_os.nodes.architect import architect_node
    from agent_os.profiles import HotContextConfig, Profile
    from agent_os.state import BackendBinding

    mock_runtime = MagicMock()
    mock_connector = MagicMock()
    mock_connector.list_notes.return_value = [{"ref": "hot.md"}]
    mock_connector.read_note.return_value = {"content": "workspace memory note content"}
    mock_runtime.memory_connector = mock_connector

    monkeypatch.setattr(
        "agent_os.server.runtime.composed_workspace",
        lambda: mock_runtime,
    )

    mock_profile = Profile(
        name="test_prof",
        hot_context=HotContextConfig(max_chars=1000, sources=["hot.md"]),
    )

    state = {
        "task": "do something",
        "messages": [],
        "backend_binding": BackendBinding(
            router=None,
            architect="cli/claude-code",
            executor="cli/codex",
            profile_name="test_prof",
            sandbox_root="/tmp/sandbox",
        ),
    }

    # Mock agent execution to avoid calling real LLM
    from agent_os.schemas import PlanArtifact

    brief_mock = PlanArtifact(summary="test")

    monkeypatch.setattr(
        "agent_os.profiles.load_profiles",
        lambda *args, **kwargs: MagicMock(),
    )
    monkeypatch.setattr(
        "agent_os.profiles.resolve_profile",
        lambda *args, **kwargs: mock_profile,
    )
    monkeypatch.setattr(
        "agent_os.nodes.architect.invoke_with_llm_retry",
        lambda fn: {"structured_response": brief_mock},
    )

    architect_node(state)
    assert mock_connector.read_note.called
