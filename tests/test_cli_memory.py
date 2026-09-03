import json
from pathlib import Path

import pytest

from agent_os.cli.app import async_main


@pytest.mark.anyio
async def test_cli_memory_index_and_status(tmp_path: Path, capsys):
    workspace_path = tmp_path / "workspace.toml"
    workspace_path.write_text(
        f"""
[workspace]
name = "mem-ws"

[backends]
architect = "codex"
executor = "codex"

[memory]
type = "markdown"
path = "{tmp_path.as_posix()}"
""",
        encoding="utf-8",
    )

    (tmp_path / "note1.md").write_text("# Note 1\n\nContent 1", encoding="utf-8")

    # 1. Run memory index
    exit_code = await async_main(["memory", "index", "--workspace", str(workspace_path)])
    assert exit_code == 0
    captured = capsys.readouterr()
    assert "Memory index complete" in captured.out or "indexed" in captured.out

    # 2. Run memory status --json
    exit_code2 = await async_main(["memory", "status", "--workspace", str(workspace_path), "--json"])
    assert exit_code2 == 0
    captured2 = capsys.readouterr()
    status_json = json.loads(captured2.out)
    assert status_json["status"] == "ready"
    assert status_json["document_count"] == 1


@pytest.mark.anyio
async def test_cli_memory_non_indexable_connector_fails_gracefully(tmp_path: Path, capsys):
    workspace_path = tmp_path / "workspace.toml"
    workspace_path.write_text(
        """
[workspace]
name = "gbrain-ws"

[backends]
architect = "codex"
executor = "codex"

[memory]
type = "gbrain"
""",
        encoding="utf-8",
    )

    exit_code = await async_main(["memory", "index", "--workspace", str(workspace_path)])
    assert exit_code != 0
    captured = capsys.readouterr()
    assert "does not support indexing" in captured.err or "does not support indexing" in captured.out
