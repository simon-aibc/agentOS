import pytest
from langchain_core.messages import AIMessage, HumanMessage

from agent_os.summarize import summarize_and_trim


def test_summarize_noop_below_threshold():
    msgs = [HumanMessage(content="hi", id="1"), AIMessage(content="hello", id="2")]

    def mock_sum(p):
        return "mock"

    summary, kept, removed = summarize_and_trim(
        msgs, "old", threshold_tokens=1000, keep_recent_n=1, summarizer=mock_sum
    )

    assert summary == "old"
    assert len(kept) == 2
    assert len(removed) == 0


def test_summarize_above_threshold():
    # Make messages long enough
    long_str = "word " * 1000
    msgs = [
        HumanMessage(content=long_str, id="1"),
        AIMessage(content="hello", id="2"),
        HumanMessage(content="hi again", id="3"),
    ]

    def mock_sum(p):
        return "new summary"

    summary, kept, removed = summarize_and_trim(
        msgs, "old summary", threshold_tokens=100, keep_recent_n=1, summarizer=mock_sum
    )

    assert summary == "new summary"
    assert len(kept) == 1
    assert kept[0].id == "3"
    assert len(removed) == 2
    assert removed[0].id == "1"
    assert removed[1].id == "2"


def test_token_not_unbounded():
    # simulate multiple turns
    def mock_sum(p):
        return "summary"

    msgs = [HumanMessage(content="w " * 100, id=str(i)) for i in range(10)]

    summary, kept, removed = summarize_and_trim(
        msgs, None, threshold_tokens=100, keep_recent_n=2, summarizer=mock_sum
    )

    assert len(kept) == 2
    assert len(removed) == 8


@pytest.mark.integration
def test_summary_preserves_gist():
    # This requires an actual LLM call.
    # The brief requires a real integration test to make sure it captures the gist.
    from agent_os.agents.cli_architect import build_cli_architect_invoker

    invoker = build_cli_architect_invoker("claude-code")

    def real_summarizer(prompt):
        fake_state = {"task": prompt, "messages": []}
        return invoker(fake_state).summary

    msgs = [
        HumanMessage(
            content="My secret password is 'OpenSesame123'. Do not forget it.", id="1"
        ),
        AIMessage(content="Got it.", id="2"),
    ]
    # Add filler messages to exceed threshold
    for i in range(20):
        msgs.append(
            HumanMessage(content="Tell me a joke about a duck." * 10, id=f"f{i}")
        )
        msgs.append(AIMessage(content="Quack quack." * 10, id=f"a{i}"))

    # Set threshold low enough to force summarization
    summary, kept, removed = summarize_and_trim(
        msgs, None, threshold_tokens=100, keep_recent_n=2, summarizer=real_summarizer
    )

    assert "password" in summary.lower() or "opensesame" in summary.lower()
    assert len(removed) > 0
