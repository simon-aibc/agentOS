import re
from unittest.mock import MagicMock

import pytest
from langgraph.errors import GraphInterrupt
from langgraph.types import Command
from pydantic import BaseModel

from agent_os.nodes.tool_dispatcher import build_tool_dispatcher_node
from agent_os.schemas import BashResult, RouterDecision
from agent_os.skills import RegisteredSkill, SkillRegistry
from agent_os.state import AgentState


class DummyResult(BaseModel):
    foo: str


def make_state(task: str) -> AgentState:
    return {
        "messages": [],
        "task": task,
        "plan": None,
        "executor_output": None,
        "human_feedback": None,
        "hot_context": None,
    }


def test_dispatcher_tier1():
    registry = SkillRegistry()

    mock_handler = MagicMock(return_value=DummyResult(foo="bar"))
    registry.register(
        RegisteredSkill(name="read_file", aliases=["read"], handler=mock_handler)
    )

    mock_llm = MagicMock()

    node = build_tool_dispatcher_node(registry=registry, router_llm=mock_llm)

    cmd = node(make_state("read path/to/f.txt"))

    assert isinstance(cmd, Command)
    assert cmd.goto == "__end__"
    assert cmd.update["tool_result"].success is True
    assert "foo" in cmd.update["tool_result"].output
    assert cmd.update["router_escalated"] is False

    mock_handler.assert_called_once_with(path="path/to/f.txt")
    mock_llm.with_structured_output.assert_not_called()


def test_dispatcher_tier2_high_confidence():
    registry = SkillRegistry()
    mock_handler = MagicMock(return_value="done")
    registry.register(
        RegisteredSkill(name="custom_tool", aliases=[], handler=mock_handler)
    )

    mock_llm = MagicMock()
    mock_structured = MagicMock()
    mock_llm.with_structured_output.return_value = mock_structured
    mock_structured.invoke.return_value = RouterDecision(
        tool="custom_tool", confidence=0.80, arguments={"a": 1}
    )

    node = build_tool_dispatcher_node(registry=registry, router_llm=mock_llm)

    cmd = node(make_state("do custom"))

    assert cmd.goto == "__end__"
    assert cmd.update["tool_result"].success is True
    mock_handler.assert_called_once_with(a=1)


def test_dispatcher_tier3_low_confidence():
    registry = SkillRegistry()
    mock_handler = MagicMock(return_value="done")
    registry.register(
        RegisteredSkill(name="custom_tool", aliases=[], handler=mock_handler)
    )

    mock_llm = MagicMock()
    mock_structured = MagicMock()
    mock_llm.with_structured_output.return_value = mock_structured
    mock_structured.invoke.return_value = RouterDecision(
        tool="custom_tool", confidence=0.79, arguments={"a": 1}
    )

    node = build_tool_dispatcher_node(registry=registry, router_llm=mock_llm)

    cmd = node(make_state("do custom"))

    assert cmd.goto == "supervisor"
    assert cmd.update["router_escalated"] is True
    assert "tool_result" not in cmd.update
    mock_handler.assert_not_called()


def test_dispatcher_tier3_unknown_tool():
    registry = SkillRegistry()

    mock_llm = MagicMock()
    mock_structured = MagicMock()
    mock_llm.with_structured_output.return_value = mock_structured
    mock_structured.invoke.return_value = RouterDecision(
        tool=None, confidence=0.0, arguments={}
    )

    node = build_tool_dispatcher_node(registry=registry, router_llm=mock_llm)

    cmd = node(make_state("do custom"))

    assert cmd.goto == "supervisor"
    assert cmd.update["router_escalated"] is True
    assert "tool_result" not in cmd.update


def test_dispatcher_tool_failure():
    registry = SkillRegistry()
    mock_handler = MagicMock(side_effect=RuntimeError("Tool crashed"))
    registry.register(
        RegisteredSkill(name="read_file", aliases=["read"], handler=mock_handler)
    )

    node = build_tool_dispatcher_node(registry=registry, router_llm=MagicMock())

    cmd = node(make_state("read path/to/f.txt"))

    assert cmd.goto == "supervisor"
    assert cmd.update["router_escalated"] is True
    assert cmd.update["tool_result"].success is False
    assert cmd.update["tool_result"].output == "Tool crashed"


def test_dispatcher_propagates_langgraph_interrupts():
    """A policy/human interrupt must pause the graph instead of escalating."""
    registry = SkillRegistry()

    def requires_approval(path: str, content: str) -> None:
        raise GraphInterrupt()

    registry.register(
        RegisteredSkill(
            name="write_file",
            aliases=["write"],
            handler=requires_approval,
        )
    )

    node = build_tool_dispatcher_node(registry=registry, router_llm=MagicMock())

    with pytest.raises(GraphInterrupt):
        node(make_state("write path/to/f.txt :: contents"))


def test_dispatcher_bash_nonzero_returncode_marks_failure():
    registry = SkillRegistry()
    mock_handler = MagicMock(
        return_value=BashResult(
            args=["python"],
            returncode=-1,
            stdout="",
            stderr="[Errno 2] No such file or directory: 'python'",
            timed_out=False,
        )
    )
    registry.register(RegisteredSkill(name="bash", aliases=[], handler=mock_handler))

    node = build_tool_dispatcher_node(registry=registry, router_llm=MagicMock())
    cmd = node(make_state("bash python"))

    assert cmd.goto == "__end__"
    assert cmd.update["tool_result"].tool == "bash"
    assert cmd.update["tool_result"].success is False
    assert '"returncode": -1' in cmd.update["tool_result"].output
    assert "No such file or directory" in cmd.update["tool_result"].output


def test_dispatcher_invokes_real_langchain_tool(tmp_path, monkeypatch):
    target = tmp_path / "sample.txt"
    target.write_text("real tool output", encoding="utf-8")
    monkeypatch.delenv("AGENT_OS_SANDBOX", raising=False)
    monkeypatch.chdir(tmp_path)

    command = build_tool_dispatcher_node()(make_state("read sample.txt"))

    assert command.goto == "__end__"
    assert command.update["tool_result"].success is True
    assert "real tool output" in command.update["tool_result"].output


def test_dispatcher_malformed_tier1_request_escalates_safely():
    command = build_tool_dispatcher_node()(make_state("write file.txt ::   "))

    assert command.goto == "supervisor"
    assert command.update["router_escalated"] is True
    assert command.update["tool_result"].tool == "write_file"
    assert command.update["tool_result"].success is False
    assert "Missing content" in command.update["tool_result"].output


def test_dispatcher_router_failure_escalates_safely():
    registry = SkillRegistry()
    registry.register(
        RegisteredSkill(
            name="custom_tool",
            aliases=[],
            handler=MagicMock(return_value="done"),
        )
    )
    mock_llm = MagicMock()
    mock_llm.with_structured_output.side_effect = RuntimeError("router unavailable")

    command = build_tool_dispatcher_node(
        registry=registry,
        router_llm=mock_llm,
    )(make_state("please do custom work"))

    assert command.goto == "supervisor"
    assert command.update["router_escalated"] is True
    assert command.update["tool_result"].tool == "router"
    assert command.update["tool_result"].success is False
    assert command.update["tool_result"].output == "router unavailable"


def test_dispatcher_output_truncation():
    registry = SkillRegistry()
    large_output = "A" * 60000
    mock_handler = MagicMock(return_value=large_output)
    registry.register(
        RegisteredSkill(
            name="large_tool",
            aliases=["large"],
            handler=mock_handler,
        )
    )

    mock_llm = MagicMock()
    mock_structured = MagicMock()
    mock_llm.with_structured_output.return_value = mock_structured
    mock_structured.invoke.return_value = RouterDecision(
        tool="large_tool", confidence=0.99, arguments={"arg": 1}
    )

    node = build_tool_dispatcher_node(registry=registry, router_llm=mock_llm)
    cmd = node(make_state("do large work"))

    assert cmd.goto == "__end__"
    assert cmd.update["tool_result"].success is True

    output = cmd.update["tool_result"].output
    match = re.match(r"^\[truncated (\d+) bytes\]\n", output)
    assert match is not None
    retained = output[match.end() :]
    assert len(output.encode("utf-8")) <= 50 * 1024
    assert int(match.group(1)) == len(large_output.encode()) - len(retained.encode())


def test_dispatcher_tier2_missing_arguments_escalates_safely():
    registry = SkillRegistry()

    def my_write(path: str, content: str):
        pass

    registry.register(RegisteredSkill(name="write_file", aliases=[], handler=my_write))

    mock_llm = MagicMock()
    mock_structured = MagicMock()
    mock_llm.with_structured_output.return_value = mock_structured

    # LLM returns a tool but omits the required 'content' argument
    mock_structured.invoke.return_value = RouterDecision(
        tool="write_file", confidence=0.99, arguments={"path": "file.txt"}
    )

    node = build_tool_dispatcher_node(registry=registry, router_llm=mock_llm)
    cmd = node(make_state("write to file.txt"))

    assert cmd.goto == "supervisor"
    assert cmd.update["router_escalated"] is True
    assert cmd.update["tool_result"].tool == "write_file"
    assert cmd.update["tool_result"].success is False
