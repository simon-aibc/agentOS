"""Black-box release coverage for the native policy-gated memory-write skill."""

import os
import subprocess
import sys
from pathlib import Path


def _isolated_cli_environment(tmp_path: Path) -> dict[str, str]:
    """Return an offline runtime environment with all durable state under tmp_path."""
    environment = os.environ.copy()
    for key in list(environment):
        if key.startswith("LLM_") or key.endswith("_API_KEY"):
            environment.pop(key)
    for key in (
        "AGENT_OS_PROFILE",
        "AGENT_OS_WORKSPACE",
        "AGENT_OS_MEMORY_CONNECTOR",
    ):
        environment.pop(key, None)

    sandbox = tmp_path / "sandbox"
    vault = tmp_path / "vault"
    sandbox.mkdir()
    vault.mkdir()
    environment.update(
        {
            "AGENT_OS_SANDBOX": str(sandbox),
            "AGENT_OS_VAULT_PATH": str(vault),
            "AGENT_OS_PERMISSIONS_DB": str(tmp_path / "permissions.db"),
            "AGENT_OS_CHECKPOINTS_DB": str(tmp_path / "checkpoints.db"),
            "LANGGRAPH_STRICT_MSGPACK": "true",
            "LITELLM_LOCAL_MODEL_COST_MAP": "True",
        }
    )
    return environment


def _run_memory_write(
    task: str,
    thread_id: str,
    environment: dict[str, str],
    *,
    feedback: str = "",
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "agent_os",
            task,
            "--thread-id",
            thread_id,
        ],
        cwd=Path(__file__).resolve().parents[1],
        env=environment,
        input=feedback,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def test_native_memory_write_cli_learns_and_reuses_persisted_approval(
    tmp_path: Path,
) -> None:
    """A fresh CLI process reuses an ``always_approve`` memory-write rule."""
    environment = _isolated_cli_environment(tmp_path)
    ref = "AI/Decisions/release.md"

    first = _run_memory_write(
        f"memory_write append {ref} :: first approved decision",
        "memory-learning-first",
        environment,
        feedback="always_approve\n",
    )
    first_output = first.stdout + first.stderr
    assert first.returncode == 0, first_output
    assert "Policy approval required:" in first_output
    assert "memory_write" in first_output
    assert (tmp_path / "vault" / ref).read_text(encoding="utf-8") == (
        "first approved decision"
    )

    # A distinct process and thread prove this is a durable learned rule, not a
    # context-local session grant. Closed stdin means any unexpected prompt
    # fails the command instead of making this test hang.
    second = _run_memory_write(
        f"memory_write append {ref} :: second approved decision",
        "memory-learning-second",
        environment,
    )
    second_output = second.stdout + second.stderr
    assert second.returncode == 0, second_output
    assert "Policy approval required:" not in second_output
    assert (tmp_path / "vault" / ref).read_text(encoding="utf-8") == (
        "first approved decision\nsecond approved decision"
    )

    listed = subprocess.run(
        [sys.executable, "-m", "agent_os", "permissions", "list", "--json"],
        cwd=Path(__file__).resolve().parents[1],
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    listed_output = listed.stdout + listed.stderr
    assert listed.returncode == 0, listed_output
    assert (
        "memory_write:write:markdown_vault:append:AI/Decisions/release.md"
        in listed.stdout
    )
