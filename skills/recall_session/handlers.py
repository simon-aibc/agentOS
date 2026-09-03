import os

from agent_os.connectors import GbrainConnector, MarkdownVaultConnector


def _get_memory_connector():
    config = os.getenv("AGENT_OS_MEMORY_CONNECTOR", "markdown")
    if config == "gbrain":
        return GbrainConnector()
    else:
        vault_path = os.getenv("AGENT_OS_VAULT_PATH", "./sandbox")
        return MarkdownVaultConnector(vault_path)


def recall_session(task: str, **kwargs) -> str:
    """
    Search session logs (AI/Logs/) for the given query and return formatted results.
    """
    query = task
    prefixes = [
        "hôm qua nói gì về ",
        "nhớ lại phiên ",
        "recall session ",
        "session log ",
    ]
    for prefix in prefixes:
        if task.lower().startswith(prefix):
            query = task[len(prefix) :].strip()
            break

    connector = _get_memory_connector()

    try:
        # Search for query
        results = connector.search(query, limit=5)
    except Exception as e:
        return f"Lỗi khi tìm kiếm session log: {e}"

    if not results:
        return f"Không tìm thấy thông tin nào trong lịch sử session cho: '{query}'"

    out = [f"Kết quả nhớ lại lịch sử chat cho '{query}':\n"]
    found = False

    for res in results:
        ref = res.get("ref", "")
        # Scope vào AI/Logs/ hoặc agentos/logs/ (theo evaluate_write_policy)
        normalized_ref = ref.lstrip("/")
        if not (
            normalized_ref.startswith("AI/Logs/")
            or normalized_ref.startswith("agentos/logs/")
        ):
            continue

        found = True
        snippet = res.get("snippet", "")
        if not snippet:
            try:
                note = connector.read_note(ref)
                snippet = note.get("content", "")[:300]
            except Exception:
                pass

        heading = res.get("source", {}).get("heading") if isinstance(res.get("source"), dict) else None
        title = res.get("title")
        header_tag = f" ({heading})" if heading else (f" ({title})" if title else "")
        out.append(f"- Phiên [{ref}]{header_tag}:\n  Trích đoạn: {snippet}...\n")

    if not found:
        return f"Không tìm thấy thông tin nào trong lịch sử session (AI/Logs/) cho: '{query}'"

    return "\n".join(out)
