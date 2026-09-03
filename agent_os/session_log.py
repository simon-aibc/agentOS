import datetime
from typing import Any

from agent_os.connectors import MemoryWriteResult, WritableMemory
from agent_os.memory_gate import gated_write
from agent_os.policy import PolicyEngine
from agent_os.schemas import MemoryWriteProposal


def write_session_summary(
    connector: WritableMemory,
    thread_id: str,
    summary: str,
    session_meta: dict[str, Any],
    *,
    engine: PolicyEngine | None = None,
) -> MemoryWriteResult:
    """Write the session summary to the vault via auto-approved append."""

    # Use UTC for standard logging date
    now = datetime.datetime.now(datetime.UTC)
    date_str = now.strftime("%Y-%m-%d")

    ref = f"AI/Logs/{date_str}.md"

    # We use append mode. For GbrainConnector/MarkdownVaultConnector,
    # if we append, does it support frontmatter?
    # WritableMemory.write_note takes frontmatter. In append mode, connectors
    # might just append the content. But we pass frontmatter anyway.

    frontmatter = {
        "agent": "agent-os",
        "created": now.isoformat(),
        "via": "session-log",
        "source": thread_id,
    }

    # Build content for the append. Since multiple sessions might append to the same file,
    # we should format it nicely.
    turn_count = session_meta.get("turn_count", 0)
    title = session_meta.get("title", "Untitled Session")

    content_lines = [
        f"\n## Session: {title} ({thread_id})",
        f"- **Time**: {now.isoformat()}",
        f"- **Turns**: {turn_count}",
        "- **Agent**: agent-os",
        "- **Via**: session-log",
        "",
        summary,
        "",
    ]

    content = "\n".join(content_lines)

    proposal = MemoryWriteProposal(
        connector=str(getattr(connector, "name", "memory")),
        ref=ref,
        mode="append",
        content_preview=summary[:100] + ("..." if len(summary) > 100 else ""),
        side_effect=f"Append session summary to {ref}",
    )

    return gated_write(
        connector, proposal, content, frontmatter=frontmatter, engine=engine
    )
