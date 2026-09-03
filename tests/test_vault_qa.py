from pathlib import Path

from agent_os.workspace import compose_workspace, load_workspace
from skills.vault_qa.handlers import vault_qa


class CustomPluginMemory:
    name = "plugin_mem"
    supported_write_modes = frozenset({"create"})

    def search(self, query: str, limit: int = 3) -> list[dict]:
        return [{"ref": "plugin://doc/1", "snippet": f"Plugin search result for: {query}"}]

    def read_note(self, ref: str) -> dict:
        return {"ref": ref, "content": f"Full content for {ref}"}

    def list_notes(self, filters: dict | None = None) -> list[dict]:
        return []

    def write_note(self, ref: str, content: str, **kwargs: object) -> object:
        return object()

    def describe_write_side_effect(self, ref: str, mode: str) -> str:
        return "write"


def test_vault_qa_with_explicit_connector():
    mem = CustomPluginMemory()
    result = vault_qa("hỏi vault: test query", connector=mem)
    assert "Plugin search result for: test query" in result
    assert "[plugin://doc/1]" in result


def test_vault_qa_with_composed_workspace(tmp_path: Path, monkeypatch):
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir()
    (vault_dir / "note1.md").write_text("# Note 1\nContent about autonomous agents in vault.\n", encoding="utf-8")

    workspace_toml = tmp_path / "workspace.toml"
    workspace_toml.write_text(
        f"""
[workspace]
name = "qa-workspace"

[backends]
architect = "codex"
executor = "codex"

[memory]
type = "markdown"
path = "{vault_dir}"
""",
        encoding="utf-8",
    )

    ws = load_workspace(workspace_toml)
    composed = compose_workspace(ws)

    # Point runtime to composed workspace
    monkeypatch.setattr("agent_os.server.runtime.composed_workspace", lambda: composed)

    result = vault_qa("hỏi vault: autonomous agents")
    assert "autonomous agents" in result
    assert "note1" in result


def test_vault_qa_with_plugin_memory_connector(tmp_path: Path, monkeypatch):
    class SyntheticEntryPoint:
        name = "custom_vault"
        value = "dummy:CustomPluginMemory"

        def load(self):
            return CustomPluginMemory

    monkeypatch.setattr(
        "agent_os.plugins._get_entry_points",
        lambda group: [SyntheticEntryPoint()] if group == "agent_os.memory_connectors" else [],
    )

    workspace_toml = tmp_path / "workspace.toml"
    workspace_toml.write_text(
        """
[workspace]
name = "plugin-qa-workspace"

[backends]
architect = "codex"
executor = "codex"

[memory]
type = "custom_vault"
""",
        encoding="utf-8",
    )

    ws = load_workspace(workspace_toml)
    composed = compose_workspace(ws)

    assert composed.memory_connector.name == "plugin_mem"

    # Set as active runtime workspace
    monkeypatch.setattr("agent_os.server.runtime.composed_workspace", lambda: composed)

    result = vault_qa("hỏi vault: neural networks")
    assert "Plugin search result for: neural networks" in result
    assert "[plugin://doc/1]" in result
