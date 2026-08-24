from unittest.mock import MagicMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.messages.utils import count_tokens_approximately

from agent_os.messages import AGENT_MESSAGE_MAX_TOKENS, trim_agent_messages
from agent_os.nodes.architect import architect_node
from agent_os.nodes.executor import executor_node
from agent_os.schemas import ArchitectBrief, ExecutorReport
from agent_os.state import AgentState


def _large_history() -> list[HumanMessage | AIMessage]:
    history: list[HumanMessage | AIMessage] = []
    for index in range(100):
        history.extend(
            [
                HumanMessage(content=f"request {index}: " + "x" * 500),
                AIMessage(content=f"response {index}: " + "y" * 500),
            ]
        )
    return history


def test_trim_agent_messages_bounds_context_without_mutating_history() -> None:
    history = _large_history()
    original = list(history)

    trimmed = trim_agent_messages(history, "Execute this immediately")

    assert len(trimmed) < len(history) + 1
    assert count_tokens_approximately(trimmed) <= AGENT_MESSAGE_MAX_TOKENS
    assert isinstance(trimmed[0], HumanMessage)
    assert isinstance(trimmed[-1], HumanMessage)
    assert trimmed[-1].content == "Execute this immediately"
    assert history == original


def test_trim_agent_messages_does_not_duplicate_current_user_task() -> None:
    current_task = HumanMessage(content="current task")

    trimmed = trim_agent_messages([current_task], "current task")

    assert trimmed == [current_task]


@pytest.mark.parametrize("node_name", ["architect", "executor"])
def test_agent_nodes_trim_messages_before_invocation(
    monkeypatch: pytest.MonkeyPatch,
    node_name: str,
) -> None:
    # Keep this unit test offline even when the developer's .env selects CLI backends.
    monkeypatch.delenv("LLM_ARCHITECT", raising=False)
    monkeypatch.delenv("LLM_EXECUTOR", raising=False)
    history = _large_history()
    original = list(history)
    mock_agent = MagicMock()

    if node_name == "architect":
        mock_agent.invoke.return_value = {
            "structured_response": ArchitectBrief(
                files=[], changes=[], verify_cmd="pytest"
            )
        }
        monkeypatch.setattr(
            "agent_os.nodes.architect.build_architect_agent",
            lambda: mock_agent,
        )
        state: AgentState = {
            "messages": history,
            "task": "do work",
            "plan": None,
            "executor_output": None,
            "human_feedback": None,
            "hot_context": None,
        }
        architect_node(state)
        expected_prompt = "do work"
    else:
        mock_agent.invoke.return_value = {
            "structured_response": ExecutorReport(
                success=True, diff="", verify_output=""
            )
        }
        monkeypatch.setattr(
            "agent_os.nodes.executor.build_executor_agent",
            lambda: mock_agent,
        )
        state = {
            "messages": history,
            "task": "do work",
            "plan": ArchitectBrief(files=[], changes=[], verify_cmd="pytest"),
            "executor_output": None,
            "human_feedback": None,
            "hot_context": None,
        }
        executor_node(state)
        expected_prompt = "Please execute this ArchitectBrief:"

    invocation = mock_agent.invoke.call_args.args[0]
    messages = invocation["messages"]
    assert count_tokens_approximately(messages) <= AGENT_MESSAGE_MAX_TOKENS
    assert isinstance(messages[-1], HumanMessage)
    assert expected_prompt in messages[-1].content
    assert history == original
