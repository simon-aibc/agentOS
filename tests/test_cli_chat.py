from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_os.checkpoints import get_checkpoint_serializer
from agent_os.cli.app import async_main, build_parser
from agent_os.state import BackendBinding


@pytest.fixture
def mock_formatter():
    fmt = MagicMock()
    return fmt


def test_run_command_unchanged():
    parser = build_parser()
    args = parser.parse_args(["run", "do something"])
    assert args.command == "run"
    assert args.task == "do something"


@pytest.mark.anyio
async def test_chat_multi_turn():
    graph = MagicMock()
    graph.aget_state = AsyncMock()
    # First turn: state not exists, second turn: state exists
    graph.aget_state.side_effect = [
        None,  # For initial check -> doesn't exist
        MagicMock(created_at="now", next=()),  # After stream pass
        MagicMock(created_at="now", next=()),  # Next loop start
        MagicMock(created_at="now", next=()),  # After stream pass
    ]

    async def mock_stream(*args, **kwargs):
        yield {"event": "mock"}

    graph.astream_events = MagicMock(side_effect=mock_stream)

    inputs = ["hi", "hello again", "/exit"]

    def input_fn(prompt):
        return inputs.pop(0)

    argv = ["chat", "--thread-id", "test-chat-1"]

    with patch("agent_os.cli.app.EventFormatter"):
        with patch(
            "agent_os.bindings.resolve_backend_binding",
            return_value=BackendBinding(
                router="r",
                architect="a",
                executor="e",
                profile_name="p",
                sandbox_root="s",
            ),
        ):
            await async_main(argv, graph_factory=lambda: graph, input_fn=input_fn)

    # _stream_pass is called twice
    assert graph.astream_events.call_count == 2

    # First call was with graph_input dict (initial state)
    first_call_input = graph.astream_events.call_args_list[0][0][0]
    assert first_call_input["task"] == "hi"
    assert first_call_input["messages"] == [("user", "hi")]

    # Second call was with Command or dict with HumanMessage
    second_call_input = graph.astream_events.call_args_list[1][0][0]
    assert "messages" in second_call_input
    assert len(second_call_input["messages"]) == 1
    assert second_call_input["messages"][0].content == "hello again"


@pytest.mark.anyio
async def test_chat_resume_after_kill():
    # Similar to above, but with --resume
    graph = MagicMock()
    snapshot = MagicMock(created_at="now", next=())
    snapshot.values = {
        "backend_binding": {
            "router": "r",
            "architect": "a",
            "executor": "e",
            "profile_name": "p",
            "sandbox_root": "s",
        }
    }

    graph.aget_state = AsyncMock()
    graph.aget_state.side_effect = [
        snapshot,  # Initial resume check
        snapshot,  # After update state
        snapshot,  # First stream pass finishes
    ]
    graph.aupdate_state = AsyncMock()

    async def mock_stream_resume(*args, **kwargs):
        yield {"event": "mock"}

    graph.astream_events = MagicMock(side_effect=mock_stream_resume)

    inputs = ["im back", "/exit"]

    def input_fn(prompt):
        return inputs.pop(0)

    argv = ["chat", "--thread-id", "test-chat-resume", "--resume"]

    with patch("agent_os.cli.app.EventFormatter"):
        with patch(
            "agent_os.bindings.resolve_backend_binding",
            return_value=BackendBinding(
                router="r",
                architect="a",
                executor="e",
                profile_name="p",
                sandbox_root="s",
            ),
        ):
            await async_main(argv, graph_factory=lambda: graph, input_fn=input_fn)

    # It should have passed a HumanMessage directly since state existed
    assert graph.astream_events.call_count == 1
    call_input = graph.astream_events.call_args_list[0][0][0]
    assert "messages" in call_input
    assert call_input["messages"][0].content == "im back"


def test_conversation_summary_default_on_old_checkpoint():
    serde = get_checkpoint_serializer()
    # Serialize a state without conversation_summary
    old_state = {
        "messages": [],
        "task": "do",
        "plan": None,
        "executor_output": None,
        "human_feedback": None,
        "hot_context": None,
        "backend_binding": {
            "router": "r",
            "architect": "a",
            "executor": "e",
            "profile_name": "p",
            "sandbox_root": "s",
        },
    }
    dumped = serde.dumps_typed(old_state)
    loaded = serde.loads_typed(dumped)

    # Actually Pydantic / TypedDict doesn't fail on missing keys if they are Optional/NotRequired
    # Since AgentState is a TypedDict with `conversation_summary: str | None`,
    # json loading it will just have it missing or we use .get("conversation_summary")
    assert (
        "conversation_summary" not in loaded or loaded["conversation_summary"] is None
    )
