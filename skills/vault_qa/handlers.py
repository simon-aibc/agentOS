from typing import Any

from agent_os.connectors import normalize_memory_hit


def vault_qa(
    task: str,
    connector: Any = None,
    memory_connector: Any = None,
    **kwargs: Any,
) -> str:
    """Search the vault for the given query and return formatted results."""
    query = task
    prefix = "hỏi vault:"
    if task.lower().startswith(prefix):
        query = task[len(prefix) :].strip()

    target_connector = (
        memory_connector
        or connector
        or kwargs.get("memory_connector")
        or kwargs.get("connector")
    )
    if target_connector is None:
        from agent_os.server.runtime import (
            memory_connector as canonical_memory_connector,
        )

        target_connector = canonical_memory_connector()

    try:
        results = target_connector.search(query, limit=3)
    except Exception as e:
        return f"Lỗi khi tra cứu vault: {e}"

    if not results:
        return f"Không tìm thấy thông tin nào trong vault cho query: '{query}'"

    out = [f"Kết quả tra cứu cho '{query}':\n"]
    for i, res in enumerate(results, 1):
        hit = normalize_memory_hit(res)
        ref = hit.ref
        snippet = hit.snippet
        if not snippet:
            try:
                note = target_connector.read_note(ref)
                if isinstance(note, dict):
                    snippet = note.get("content", "")[:200]
                else:
                    snippet = str(getattr(note, "content", note))[:200]
            except Exception:
                pass

        block = f"{i}. [{ref}]\nTrích đoạn: {snippet}..."
        if hit.source:
            source_parts = [f"{k}={v}" for k, v in sorted(hit.source.items())]
            block += f"\nNguồn: {', '.join(source_parts)}"

        out.append(block + "\n")

    return "\n".join(out)
