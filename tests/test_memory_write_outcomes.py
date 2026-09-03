"""Regression tests for preserving policy outcomes at memory-write boundaries."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from agent_os.brief_runtime import BriefExecutionResult, execute_brief
from agent_os.cli.app import async_main
from agent_os.connectors import MarkdownVaultConnector, MemoryWriteResult
from agent_os.memory_gate import gated_write
from agent_os.permission_store import InMemoryPermissionStore, PermissionRule
from agent_os.policy import LocalPolicy, derive_permission_key
from agent_os.scheduler import dispatch_schedule
from agent_os.schemas import ActionProposal, MemoryWriteProposal
from agent_os.tools.memory_write import build_memory_write_handler


def _decision_proposal() -> MemoryWriteProposal:
    return MemoryWriteProposal(
        connector="markdown_vault",
        ref="AI/Decisions/outcome.md",
        mode="create",
        content_preview="A decision",
        side_effect="write a decision",
    )


def test_gated_write_exposes_rejection_reason(tmp_path) -> None:
    connector = MarkdownVaultConnector(str(tmp_path / "vault"))

    with patch("agent_os.policy.interrupt", return_value="rejected: not now"):
        result = gated_write(connector, _decision_proposal(), "content")

    assert result.committed is False
    assert result.status == "cancelled"
    assert result.error == "rejected: not now"
    assert not (tmp_path / "vault" / "AI" / "Decisions" / "outcome.md").exists()


def test_gated_write_exposes_learned_policy_denial_reason(tmp_path) -> None:
    connector = MarkdownVaultConnector(str(tmp_path / "vault"))
    store = InMemoryPermissionStore()
    action = ActionProposal(
        tool="memory.write",
        connector="markdown_vault",
        arguments={"ref": "AI/Decisions/outcome.md", "mode": "create"},
        side_effect="write",
    )
    permission_key = derive_permission_key(action)
    assert permission_key is not None
    store.upsert(
        PermissionRule(
            permission_key=permission_key,
            effect="always_deny",
            tier_at_creation="low",
        )
    )

    result = gated_write(
        connector,
        _decision_proposal(),
        "content",
        engine=LocalPolicy(store=store),
    )

    assert result.committed is False
    assert result.status == "failed"
    assert result.error == "always_deny"
    assert not (tmp_path / "vault" / "AI" / "Decisions" / "outcome.md").exists()


def test_gated_write_exposes_remembered_permission_store_failure(tmp_path) -> None:
    class FailingStore:
        def get(self, permission_key: str):
            return None

        def upsert(self, rule) -> None:
            raise OSError("database is read-only")

    connector = MarkdownVaultConnector(str(tmp_path / "vault"))
    policy = LocalPolicy(store=FailingStore())

    with patch("agent_os.policy.interrupt", return_value="always_approve"):
        result = gated_write(
            connector,
            _decision_proposal(),
            "content",
            engine=policy,
        )

    assert result.committed is False
    assert result.status == "cancelled"
    assert result.error is not None
    assert "Could not save the remembered permission" in result.error
    assert not (tmp_path / "vault" / "AI" / "Decisions" / "outcome.md").exists()


def test_native_memory_skill_preserves_the_policy_failure_reason(
    monkeypatch, tmp_path
) -> None:
    connector = MarkdownVaultConnector(str(tmp_path / "vault"))
    rejected = MemoryWriteResult(
        ref="AI/Decisions/outcome.md",
        mode="create",
        bytes_written=None,
        committed=False,
        status="cancelled",
        error="rejected: not now",
    )
    monkeypatch.setattr(
        "agent_os.tools.memory_write.gated_write",
        lambda *_args, **_kwargs: rejected,
    )

    result = build_memory_write_handler(connector)(
        "AI/Decisions/outcome.md", "content", "create"
    )

    assert result.status == "cancelled"
    assert result.errors == ["rejected: not now"]


def test_execute_brief_does_not_expose_a_ref_for_an_uncommitted_write(
    monkeypatch,
    tmp_path,
) -> None:
    failed_write = MemoryWriteResult(
        ref="AI/Briefs/2026-08-18.md",
        mode="create",
        bytes_written=None,
        committed=False,
        status="cancelled",
        error="rejected: brief not wanted",
    )
    connector = MarkdownVaultConnector(str(tmp_path / "vault"))

    monkeypatch.setattr("agent_os.brief_runtime.list_sessions", lambda: [])
    monkeypatch.setattr(
        "agent_os.brief_runtime.build_brief_summarizer",
        lambda: lambda _prompt: "# Brief",
    )
    monkeypatch.setattr(
        "agent_os.brief_runtime.write_brief", lambda *_args, **_kwargs: failed_write
    )

    result = execute_brief(date="2026-08-18", connector=connector, write=True)

    assert result.saved is False
    assert result.ref is None
    assert result.write_result is failed_write
    assert result.error == "rejected: brief not wanted"


@pytest.mark.anyio
async def test_scheduler_marks_denied_brief_write_as_error(monkeypatch) -> None:
    denied = BriefExecutionResult(
        date="2026-08-18",
        content="# Brief",
        write_result=MemoryWriteResult(
            ref="AI/Briefs/2026-08-18.md",
            mode="create",
            bytes_written=None,
            committed=False,
            status="cancelled",
            error="rejected: brief not wanted",
        ),
        error="rejected: brief not wanted",
    )
    records: list[tuple[str, str, str | None]] = []

    monkeypatch.setattr("agent_os.scheduler.execute_brief", lambda **_kwargs: denied)
    monkeypatch.setattr(
        "agent_os.scheduler.record_schedule_result",
        lambda schedule_id, *, status, error=None: records.append(
            (schedule_id, status, error)
        ),
    )

    result = await dispatch_schedule(
        {"schedule_id": "brief-denied", "kind": "brief", "payload": {}}
    )

    assert result.status == "error"
    assert result.ref is None
    assert result.error == "rejected: brief not wanted"
    assert records == [("brief-denied", "error", "rejected: brief not wanted")]


@pytest.mark.anyio
async def test_brief_cli_does_not_claim_an_uncommitted_write_was_saved(
    monkeypatch,
    tmp_path,
    capsys,
) -> None:
    denied = BriefExecutionResult(
        date="2026-08-18",
        content="# Brief",
        write_result=MemoryWriteResult(
            ref="AI/Briefs/2026-08-18.md",
            mode="create",
            bytes_written=None,
            committed=False,
            status="cancelled",
            error="rejected: brief not wanted",
        ),
        error="rejected: brief not wanted",
    )
    monkeypatch.setenv("AGENT_OS_PERMISSIONS_DB", str(tmp_path / "permissions.db"))
    monkeypatch.setattr(
        "agent_os.brief_runtime.execute_brief", lambda **_kwargs: denied
    )

    exit_code = await async_main(["brief", "--date", "2026-08-18"])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "Morning Brief generated and saved" not in captured.out
    assert "Morning Brief was generated but not saved" in captured.err
    assert "rejected: brief not wanted" in captured.err
