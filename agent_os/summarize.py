from collections.abc import Callable, Sequence

from langchain_core.messages import AnyMessage
from langchain_core.messages.modifier import RemoveMessage


def _approximate_tokens(text: str) -> int:
    return len(text) // 4


def _count_message_tokens(messages: Sequence[AnyMessage]) -> int:
    total = 0
    for msg in messages:
        if isinstance(msg.content, str):
            total += _approximate_tokens(msg.content)
        elif isinstance(msg.content, list):
            for block in msg.content:
                if isinstance(block, str):
                    total += _approximate_tokens(block)
                elif isinstance(block, dict) and "text" in block:
                    total += _approximate_tokens(block["text"])
    return total


def summarize_and_trim(
    messages: Sequence[AnyMessage],
    existing_summary: str | None,
    *,
    threshold_tokens: int,
    keep_recent_n: int,
    summarizer: Callable[[str], str],
) -> tuple[str | None, list[AnyMessage], list[RemoveMessage]]:
    """
    Checks if messages exceed the threshold. If so, summarizes the old messages and trims them.

    Returns:
        (new_summary, kept_messages, remove_messages)
    """
    total_tokens = _count_message_tokens(messages)
    if total_tokens < threshold_tokens or len(messages) <= keep_recent_n:
        return existing_summary, list(messages), []

    # Exceeds threshold, need to trim
    split_idx = len(messages) - keep_recent_n
    if split_idx <= 0:
        return existing_summary, list(messages), []

    old_messages = messages[:split_idx]
    kept_messages = list(messages[split_idx:])

    # Format old messages for summarization
    summary_prompt = "Summarize the following conversation history.\n"
    if existing_summary:
        summary_prompt += f"Existing Summary:\n{existing_summary}\n\n"

    summary_prompt += "New conversation turns to summarize:\n"
    for m in old_messages:
        content = m.content if isinstance(m.content, str) else str(m.content)
        summary_prompt += f"[{m.type}]: {content}\n"

    new_summary = summarizer(summary_prompt)

    remove_messages = [RemoveMessage(id=m.id) for m in old_messages if m.id]

    return new_summary, kept_messages, remove_messages
