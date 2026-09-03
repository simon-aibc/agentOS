import io
import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from rich.console import Console

from agent_os.cli.app import _read_feedback, async_main
from agent_os.cli.formatter import EventFormatter
from agent_os.permission_store import PermissionRule, SqlitePermissionStore
from agent_os.policy import POLICY_APPROVAL_PROMPT_PREFIX
from agent_os.server.api import PERMISSIONS_ADMIN_TOKEN_ENV, app


def _workspace_file(root: Path, name: str) -> Path:
    root.mkdir()
    path = root / "workspace.toml"
    path.write_text(f'[workspace]\nname = "{name}"\n', encoding="utf-8")
    return path


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        ("session", "session"),
        ("always_approve", "always_approve"),
        ("always_deny", "always_deny"),
    ],
)
def test_cli_accepts_policy_learning_feedback(response: str, expected: str) -> None:
    console = Console(file=io.StringIO(), force_terminal=False, color_system=None)
    formatter = EventFormatter(console=console)

    feedback = _read_feedback(
        f"{POLICY_APPROVAL_PROMPT_PREFIX} policy proposal",
        formatter,
        lambda _: response,
    )

    assert feedback == expected


@pytest.mark.anyio
async def test_permissions_cli_uses_workspace_store(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.delenv("AGENT_OS_PERMISSIONS_DB", raising=False)
    workspace_path = _workspace_file(tmp_path / "workspace", "cli-permissions")
    key = "memory_write:write:markdown_vault:create:AI/Decisions/cli.md"
    store = SqlitePermissionStore(str(workspace_path.parent / "permissions.db"))
    store.upsert(
        PermissionRule(
            permission_key=key,
            effect="always_approve",
            tier_at_creation="low",
        )
    )

    output = io.StringIO()
    console = Console(file=output, force_terminal=False, color_system=None)

    listed = await async_main(
        ["permissions", "list", "--workspace", str(workspace_path)],
        console=console,
    )
    assert listed == 0
    assert key in output.getvalue()

    revoked = await async_main(
        ["permissions", "revoke", key, "--workspace", str(workspace_path)],
        console=console,
    )
    assert revoked == 0
    assert store.get(key) is None


def test_permissions_api_requires_token_and_uses_active_workspace(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from agent_os.server import runtime

    monkeypatch.delenv("AGENT_OS_PERMISSIONS_DB", raising=False)
    workspace_path = _workspace_file(tmp_path / "workspace", "api-permissions")
    key = "memory_write:write:markdown_vault:create:AI/Decisions/api.md"
    store = SqlitePermissionStore(str(workspace_path.parent / "permissions.db"))
    store.upsert(
        PermissionRule(
            permission_key=key,
            effect="always_approve",
            tier_at_creation="low",
        )
    )
    monkeypatch.setenv("AGENT_OS_WORKSPACE", str(workspace_path))
    monkeypatch.setenv(PERMISSIONS_ADMIN_TOKEN_ENV, "permissions-test-token")
    runtime.composed_workspace.cache_clear()

    try:
        client = TestClient(app)
        assert client.get("/api/permissions").status_code == 403

        headers = {"x-admin-token": "permissions-test-token"}
        listed = client.get("/api/permissions", headers=headers)
        assert listed.status_code == 200
        assert [rule["permission_key"] for rule in listed.json()] == [key]

        revoked = client.delete(f"/api/permissions/{key}", headers=headers)
        assert revoked.status_code == 200
        assert store.get(key) is None
    finally:
        runtime.composed_workspace.cache_clear()


def test_permissions_cli_subprocess_reads_and_revokes_persisted_rule(
    tmp_path: Path,
) -> None:
    """A fresh CLI process manages the same workspace-owned SQLite rule."""
    workspace_path = _workspace_file(tmp_path / "workspace", "subprocess-permissions")
    key = "memory_write:write:markdown_vault:create:AI/Decisions/subprocess.md"
    store = SqlitePermissionStore(str(workspace_path.parent / "permissions.db"))
    store.upsert(
        PermissionRule(
            permission_key=key,
            effect="always_approve",
            tier_at_creation="low",
        )
    )
    environment = os.environ.copy()
    environment.pop("AGENT_OS_PERMISSIONS_DB", None)
    project_root = Path(__file__).resolve().parents[1]
    command = [
        sys.executable,
        "-m",
        "agent_os",
        "permissions",
        "list",
        "--workspace",
        str(workspace_path),
    ]

    listed = subprocess.run(
        command,
        cwd=project_root,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert listed.returncode == 0, listed.stderr
    assert key in listed.stdout

    revoked = subprocess.run(
        [
            sys.executable,
            "-m",
            "agent_os",
            "permissions",
            "revoke",
            key,
            "--workspace",
            str(workspace_path),
        ],
        cwd=project_root,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert revoked.returncode == 0, revoked.stderr
    assert store.get(key) is None
