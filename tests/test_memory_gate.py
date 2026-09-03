from unittest.mock import patch

import pytest

from agent_os.checkpoints import CHECKPOINT_MODEL_TYPES
from agent_os.connectors import GbrainConnector, MarkdownVaultConnector
from agent_os.memory_gate import evaluate_write_policy, gated_write
from agent_os.permission_store import InMemoryPermissionStore
from agent_os.policy import LocalPolicy
from agent_os.schemas import MemoryWriteProposal


def test_policy_logs_append_auto():
    assert evaluate_write_policy("AI/Logs/x.md", "append") == "auto"
    assert evaluate_write_policy("/AI/Logs/x.md", "append") == "auto"
    assert evaluate_write_policy("agentos/logs/x.md", "append") == "auto"


def test_policy_decisions_requires_gate():
    assert evaluate_write_policy("AI/Decisions/x.md", "create") == "gate"
    assert evaluate_write_policy("AI/Decisions/x.md", "append") == "gate"
    assert evaluate_write_policy("AI/Decisions/x.md", "overwrite") == "gate"


def test_policy_overwrite_requires_gate():
    assert evaluate_write_policy("AI/Logs/x.md", "overwrite") == "gate"
    assert evaluate_write_policy("AI/Logs/x.md", "create") == "gate"


def test_memory_write_proposal_in_allowlist():
    assert MemoryWriteProposal in CHECKPOINT_MODEL_TYPES


def test_gated_write_auto_commits(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    connector = MarkdownVaultConnector(str(vault))

    proposal = MemoryWriteProposal(
        connector="markdown_vault",
        ref="AI/Logs/x.md",
        mode="append",
        content_preview="preview",
        side_effect="side effect",
    )

    res = gated_write(connector, proposal, "real content")
    assert res.committed is True
    assert (vault / "AI/Logs/x.md").read_text(encoding="utf-8") == "real content"


def test_gated_write_approved(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    connector = MarkdownVaultConnector(str(vault))

    proposal = MemoryWriteProposal(
        connector="markdown_vault",
        ref="AI/Decisions/x.md",
        mode="create",
        content_preview="preview",
        side_effect="side effect",
    )

    with patch("agent_os.policy.interrupt", return_value="approved"):
        res = gated_write(connector, proposal, "real content")

    assert res.committed is True
    assert (vault / "AI/Decisions/x.md").read_text(encoding="utf-8") == "real content"


def test_gated_write_rejected(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    connector = MarkdownVaultConnector(str(vault))

    proposal = MemoryWriteProposal(
        connector="markdown_vault",
        ref="AI/Decisions/x.md",
        mode="create",
        content_preview="preview",
        side_effect="side effect",
    )

    with patch("agent_os.policy.interrupt", return_value="rejected: no"):
        res = gated_write(connector, proposal, "real content")

    assert res.committed is False
    assert res.bytes_written is None
    assert not (vault / "AI/Decisions/x.md").exists()


def test_gated_write_preserves_connector_execution_error():
    """A connector failure is not converted into an indistinguishable denial."""

    class ExplodingMemory:
        name = "markdown_vault"

        def write_note(self, *args, **kwargs):
            raise RuntimeError("disk unavailable")

    proposal = MemoryWriteProposal(
        connector="markdown_vault",
        ref="AI/Logs/x.md",
        mode="append",
        content_preview="preview",
        side_effect="side effect",
    )

    with pytest.raises(RuntimeError, match="disk unavailable"):
        gated_write(ExplodingMemory(), proposal, "real content")


def test_gated_write_rejects_unsupported_gbrain_mode_before_learning():
    """An upsert-only connector cannot acquire a misleading create grant."""
    store = InMemoryPermissionStore()
    proposal = MemoryWriteProposal(
        connector="gbrain",
        ref="AI/Decisions/existing-page",
        mode="create",
        content_preview="replacement",
        side_effect="side effect",
    )

    with patch("agent_os.policy.interrupt", return_value="always_approve"):
        result = gated_write(
            GbrainConnector(),
            proposal,
            "replacement",
            engine=LocalPolicy(store=store, session_key="gbrain-test"),
        )

    assert result.committed is False
    assert result.status == "failed"
    assert "does not safely support mode 'create'" in (result.error or "")
    assert store.list() == []
