from unittest.mock import MagicMock

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from agent_os.graph import build_graph
from agent_os.nodes.tool_dispatcher import build_tool_dispatcher_node
from agent_os.routing import build_runtime_config, get_router_mode
from agent_os.schemas import ArchitectBrief
from agent_os.skills import RegisteredSkill, SkillRegistry
from agent_os.state import AgentState


def make_state(task: str) -> AgentState:
    return {
        "messages": [],
        "task": task,
        "plan": None,
        "executor_output": None,
        "human_feedback": None,
        "hot_context": None,
    }


def test_router_mode_defaults_to_cascade(monkeypatch):
    monkeypatch.delenv("ROUTER_MODE", raising=False)

    assert get_router_mode() == "cascade"


def test_router_mode_accepts_explicit_cascade(monkeypatch):
    monkeypatch.setenv("ROUTER_MODE", "cascade")

    assert get_router_mode() == "cascade"


def test_router_mode_rejects_unknown_value(monkeypatch):
    monkeypatch.setenv("ROUTER_MODE", "unexpected")

    with pytest.raises(ValueError, match="Invalid ROUTER_MODE"):
        get_router_mode()


def test_direct_escalation_skips_tier2_on_tier1_miss(monkeypatch):
    monkeypatch.setenv("ROUTER_MODE", "direct-escalation")
    monkeypatch.setenv("LLM_ROUTER", "invalid/must-not-be-read")
    registry = SkillRegistry()
    registry.register(
        RegisteredSkill(
            name="semantic_tool",
            aliases=[],
            handler=MagicMock(return_value="unused"),
        )
    )
    get_router_llm = MagicMock(side_effect=AssertionError("Tier-2 was built"))
    monkeypatch.setattr(
        "agent_os.nodes.smart_router.get_router_llm",
        get_router_llm,
    )
    node = build_tool_dispatcher_node(registry=registry)

    command = node(make_state("add a docstring to example.py"))

    assert command.goto == "supervisor"
    assert command.update["router_escalated"] is True
    get_router_llm.assert_not_called()


def test_direct_escalation_preserves_tier1_hit(monkeypatch):
    monkeypatch.setenv("ROUTER_MODE", "direct-escalation")
    handler = MagicMock(return_value="done")
    registry = SkillRegistry()
    registry.register(
        RegisteredSkill(name="read_file", aliases=["read"], handler=handler)
    )
    router_llm = MagicMock()

    command = build_tool_dispatcher_node(
        registry=registry,
        router_llm=router_llm,
    )(make_state("read example.py"))

    assert command.goto == "__end__"
    handler.assert_called_once_with(path="example.py")
    router_llm.with_structured_output.assert_not_called()


def test_direct_escalation_still_reaches_human_gate(monkeypatch):
    monkeypatch.setenv("ROUTER_MODE", "direct-escalation")

    def architect(state: AgentState):
        return {
            "plan": ArchitectBrief(
                files=["example.py"],
                changes=["add a docstring"],
                verify_cmd="python -m py_compile example.py",
            )
        }

    graph = build_graph(
        architect_node_impl=architect,
        tool_dispatcher_node_impl=build_tool_dispatcher_node(
            registry=SkillRegistry(),
            router_llm=MagicMock(),
        ),
        checkpointer=InMemorySaver(),
    )
    config = build_runtime_config("direct-human-gate")

    result = graph.invoke(make_state("add a docstring to example.py"), config=config)

    assert "__interrupt__" in result
    assert graph.get_state(config).next == ("human_gate",)
