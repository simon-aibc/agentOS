import datetime
from collections.abc import Callable
from typing import Any

from agent_os.connectors import MemoryConnector, MemoryWriteResult, WritableMemory
from agent_os.hot_context import load_hot_context
from agent_os.memory_gate import MemoryWriteProposal, gated_write
from agent_os.policy import PolicyEngine


def generate_brief(
    memory_connector: MemoryConnector,
    sessions: list[dict[str, Any]],
    *,
    date: str,
    summarizer: Callable[[str], str],
    spine_sources: list[str] | None = None,
) -> str:
    """
    Generate a morning brief by reading yesterday's logs, context spine, and active sessions.

    Args:
        memory_connector: Memory connector to read vault.
        sessions: List of sessions dicts from the sessions table.
        date: Target date string (e.g. '2026-08-07'). Yesterday will be calculated from this.
        summarizer: LLM function to summarize the brief.
        spine_sources: Override for hot context sources.
    """
    # 1. Date calculation
    try:
        dt = datetime.datetime.fromisoformat(date)
    except ValueError:
        # Fallback if just YYYY-MM-DD
        dt = datetime.datetime.strptime(date, "%Y-%m-%d")

    yesterday = (dt - datetime.timedelta(days=1)).strftime("%Y-%m-%d")

    # 2. Read logs from yesterday
    logs_content = ""
    try:
        # Try to read the exact log file for yesterday
        ref = f"AI/Logs/{yesterday}.md"
        note = memory_connector.read_note(ref)
        logs_content = note.get("content", "")
    except Exception:
        # If specific file not found, try searching or list_notes in AI/Logs/
        try:
            for note in memory_connector.list_notes():
                ref = note.get("ref", "")
                if ref.startswith("AI/Logs/") and yesterday in ref:
                    logs_content += note.get("content", "") + "\n"
        except Exception:
            pass

    # 3. Read context spine
    spine_content = load_hot_context(memory_connector, sources=spine_sources)

    # 4. Format sessions
    sessions_content = ""
    if sessions:
        lines = []
        for s in sessions:
            title = s.get("title") or s.get("thread_id") or "Untitled"
            updated = s.get("updated_at", "")
            lines.append(f"- {title} (Last updated: {updated})")
        sessions_content = "\n".join(lines)

    # 5. Build prompt for LLM
    prompt = f"""You are a helpful AI assistant preparing a Morning Brief for the user for the date {date}.
Using the provided context, generate a concise Markdown briefing containing exactly these 3 sections:
1. **Hôm qua**: What was accomplished yesterday (based on Yesterday's Logs).
2. **Focus hôm nay**: What to focus on today (based on the Context Spine / Goals / Invariants).
3. **Threads đang chờ**: Active or pending tasks (based on Active Sessions).

If a section has no data, just say "Không có dữ liệu".

<yesterdays_logs>
{logs_content}
</yesterdays_logs>

<context_spine>
{spine_content}
</context_spine>

<active_sessions>
{sessions_content}
</active_sessions>

Respond ONLY with the Markdown content for the brief.
"""

    # 6. Generate
    brief_md = summarizer(prompt)
    return brief_md


def write_brief(
    connector: WritableMemory,
    brief_md: str,
    date: str,
    *,
    engine: PolicyEngine | None = None,
) -> MemoryWriteResult:
    """
    Write the generated brief to the vault via gated_write (which auto-approves AI/Briefs/).
    """
    ref = f"AI/Briefs/{date}.md"

    now = datetime.datetime.now(datetime.UTC)
    frontmatter = {
        "agent": "agent-os",
        "created": now.isoformat(),
        "via": "morning-brief",
    }

    # We use append mode if we want to add to it, but typically a brief is created per day.
    # We will use "append" so it can be auto-approved, or "create" if it is auto-approved.
    # The policy for AI/Logs/ allows both append and create.
    # Let's use "create" mode for briefs as there's 1 per day.
    # Wait, the spec says AI/Briefs/ append/create -> "auto".

    proposal = MemoryWriteProposal(
        connector=str(getattr(connector, "name", "memory")),
        ref=ref,
        mode="create",
        content_preview=brief_md[:100] + "...",
        side_effect=f"Create morning brief for {date}",
    )

    # Write using gated_write which checks evaluate_write_policy
    # (wait, gated_write signature: gated_write(connector, proposal, content, frontmatter))
    return gated_write(connector, proposal, brief_md, frontmatter, engine=engine)
