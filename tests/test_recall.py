import pytest

from agent_os.connectors import MarkdownVaultConnector
from agent_os.session_log import write_session_summary
from skills.recall_session.handlers import recall_session


def test_write_session_summary_auto(tmp_path):
    """Test that write_session_summary auto-approves and writes correctly to AI/Logs/"""
    vault_path = tmp_path / "vault"
    vault_path.mkdir()

    connector = MarkdownVaultConnector(str(vault_path))

    thread_id = "thread-123"
    summary = "We discussed the weather and AI models."
    session_meta = {"turn_count": 5, "title": "Weather talk"}

    res = write_session_summary(connector, thread_id, summary, session_meta)

    assert res.committed is True
    assert "AI/Logs/" in res.ref

    # Read the file
    content = (vault_path / res.ref).read_text()

    # Check content
    assert summary in content
    assert "Weather talk" in content

    # Check frontmatter was written
    assert "agent-os" in content
    assert "session-log" in content
    assert thread_id in content


def test_recall_session_finds_prior(tmp_path, monkeypatch):
    """Test that recall_session finds the right session and scopes to AI/Logs/"""
    vault_path = tmp_path / "vault"
    vault_path.mkdir()
    monkeypatch.setenv("AGENT_OS_MEMORY_CONNECTOR", "markdown")
    monkeypatch.setenv("AGENT_OS_VAULT_PATH", str(vault_path))

    connector = MarkdownVaultConnector(str(vault_path))

    class MockDatetime:
        @classmethod
        def now(cls, tz=None):
            return cls.current

    import datetime

    # Write session 1
    MockDatetime.current = datetime.datetime(2026, 8, 1, tzinfo=datetime.UTC)
    with monkeypatch.context() as m:
        m.setattr(datetime, "datetime", MockDatetime)
        write_session_summary(
            connector,
            "thread-1",
            "We decided to use PostgreSQL for the database.",
            {"title": "DB Selection"},
        )

    # Write session 2
    MockDatetime.current = datetime.datetime(2026, 8, 2, tzinfo=datetime.UTC)
    with monkeypatch.context() as m:
        m.setattr(datetime, "datetime", MockDatetime)
        write_session_summary(
            connector,
            "thread-2",
            "We discussed using React for the frontend.",
            {"title": "UI Selection"},
        )

    # Write a non-log file
    connector.write_note("Knowledge/Frontend.md", "React is good.", mode="create")

    # Search for PostgreSQL
    res = recall_session("nhớ lại phiên PostgreSQL")
    assert "thread-1" in res or "DB Selection" in res
    assert "React" not in res

    # Search for React
    res2 = recall_session("hôm qua nói gì về React")
    assert "thread-2" in res2 or "UI Selection" in res2
    assert "Knowledge/Frontend.md" not in res2  # Should be scoped to AI/Logs/


@pytest.mark.integration
def test_smoke_recall_real(tmp_path, monkeypatch):
    """Real integration test for recall session (requires LLM config for embedding/search if applicable, but MarkdownVaultConnector uses basic search or embedding).
    We will just test using MarkdownVaultConnector."""
    vault_path = tmp_path / "vault"
    vault_path.mkdir()
    monkeypatch.setenv("AGENT_OS_MEMORY_CONNECTOR", "markdown")
    monkeypatch.setenv("AGENT_OS_VAULT_PATH", str(vault_path))

    connector = MarkdownVaultConnector(str(vault_path))

    write_session_summary(
        connector,
        "thread-smoke",
        "The secret codeword is 'BananaHammock'.",
        {"title": "Secret Meeting"},
    )

    res = recall_session("hôm qua nói gì về BananaHammock")
    assert "BananaHammock" in res
    assert "Secret Meeting" in res
