"""A native, policy-gated memory-write skill for the shipped runtime."""

import os
from collections.abc import Callable

from agent_os.connectors import (
    GbrainConnector,
    MarkdownVaultConnector,
    WritableMemory,
)
from agent_os.memory_gate import gated_write
from agent_os.sandbox import get_sandbox_root
from agent_os.schemas import ExecutionResult, MemoryWriteMode, MemoryWriteProposal


def default_memory_connector() -> WritableMemory:
    """Build the configured memory connector for a non-workspace runtime."""
    if os.getenv("AGENT_OS_MEMORY_CONNECTOR", "").strip().lower() == "gbrain":
        return GbrainConnector()
    vault_path = os.getenv("AGENT_OS_VAULT_PATH") or str(get_sandbox_root())
    return MarkdownVaultConnector(vault_path)


def build_memory_write_handler(
    connector: WritableMemory,
) -> Callable[[str, str, MemoryWriteMode], ExecutionResult]:
    """Build a skill handler bound to a concrete runtime memory connector."""

    def memory_write(
        ref: str,
        content: str,
        mode: MemoryWriteMode = "create",
    ) -> ExecutionResult:
        proposal = MemoryWriteProposal(
            connector=connector.name,
            ref=ref,
            mode=mode,
            content_preview=content[:100] + ("..." if len(content) > 100 else ""),
            side_effect=connector.describe_write_side_effect(ref, mode),
        )
        result = gated_write(connector, proposal, content)
        if result.committed:
            return ExecutionResult(
                status="completed",
                outputs={
                    "ref": result.ref,
                    "mode": result.mode,
                    "bytes_written": result.bytes_written,
                },
            )

        error = getattr(result, "error", None)
        errors = [str(error)] if isinstance(error, str) and error else []
        status = getattr(result, "status", None)
        if status not in {"failed", "cancelled"}:
            status = "cancelled"
        return ExecutionResult(
            status=status,
            outputs={"ref": result.ref, "mode": result.mode},
            errors=errors or ["Memory write was not committed"],
        )

    return memory_write
