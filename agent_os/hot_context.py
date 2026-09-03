import fnmatch
from datetime import UTC, datetime

from agent_os.connectors import MemoryConnector


def load_hot_context(
    connector: MemoryConnector,
    *,
    max_chars: int = 8000,
    max_age_days: int = 14,
    sources: list[str] | None = None,
) -> str:
    if sources is None:
        sources = ["system.md", "invariants.md", "goals.md", "hot.md", "AI/Memory/*.md"]

    try:
        all_notes = connector.list_notes()
    except Exception:
        return ""

    matching_refs = []
    for src in sources:
        for note in all_notes:
            ref = note.get("ref", "")
            if not ref:
                continue
            if fnmatch.fnmatch(ref, src) and ref not in matching_refs:
                matching_refs.append(ref)

    content_parts = []
    current_chars = 0
    now = datetime.now(UTC).timestamp()

    for ref in matching_refs:
        if current_chars >= max_chars:
            break

        try:
            note = connector.read_note(ref)
        except Exception:
            continue

        is_too_old = False
        if getattr(connector, "name", "") == "markdown_vault":
            # For markdown, we can check mtime directly
            try:
                root_path = connector.root_path
                path = root_path / ref
                if path.exists():
                    mtime = path.stat().st_mtime
                    age_days = (now - mtime) / (24 * 3600)
                    if age_days > max_age_days:
                        is_too_old = True
            except Exception:
                pass
        else:
            fm = note.get("frontmatter", {})
            date_str = (
                fm.get("effective_date") or fm.get("updated") or fm.get("created")
            )
            if date_str:
                try:
                    dt = datetime.fromisoformat(str(date_str).replace("Z", "+00:00"))
                    age_days = (now - dt.timestamp()) / (24 * 3600)
                    if age_days > max_age_days:
                        is_too_old = True
                except Exception:
                    pass

        if is_too_old:
            continue

        content = note.get("content", "")
        if not content:
            continue

        part = f"--- BEGIN {ref} ---\n{content}\n--- END {ref} ---\n"

        # Account for the newline join if this is not the first part
        join_len = 1 if current_chars > 0 else 0
        remaining_chars = max_chars - current_chars - join_len

        if remaining_chars <= 0:
            break

        if len(part) > remaining_chars:
            part = part[:remaining_chars]

        content_parts.append(part)
        current_chars += len(part) + join_len

    return "\n".join(content_parts)
