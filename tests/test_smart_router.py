from unittest.mock import MagicMock

from agent_os.nodes.smart_router import classify_tool_request
from agent_os.schemas import RouterDecision
from agent_os.skills import RegisteredSkill, SkillRegistry


def test_classify_tool_request_high_confidence():
    registry = SkillRegistry()
    registry.register(RegisteredSkill(name="my_tool", aliases=[], handler=lambda x: x))

    mock_llm = MagicMock()
    mock_structured = MagicMock()
    mock_llm.with_structured_output.return_value = mock_structured

    mock_structured.invoke.return_value = RouterDecision(
        tool="my_tool", confidence=0.9, arguments={"x": 1}
    )

    decision = classify_tool_request("do something", registry, llm=mock_llm)

    assert decision.tool == "my_tool"
    assert decision.confidence == 0.9
    assert decision.arguments == {"x": 1}

    # Verify prompt includes the tool
    mock_llm.with_structured_output.assert_called_once_with(RouterDecision)
    called_messages = mock_structured.invoke.call_args[0][0]
    system_prompt = called_messages[0].content
    assert "- my_tool" in system_prompt
    assert "input schemas" in system_prompt
    assert "x" in system_prompt
    assert '"add docstring to X"' in system_prompt
    assert "tool: null" in system_prompt
    assert "required arguments are missing" in system_prompt


def test_classify_tool_request_unknown_tool_rejection():
    registry = SkillRegistry()
    registry.register(RegisteredSkill(name="my_tool", aliases=[], handler=lambda: None))

    mock_llm = MagicMock()
    mock_structured = MagicMock()
    mock_llm.with_structured_output.return_value = mock_structured

    # LLM invents a tool
    mock_structured.invoke.return_value = RouterDecision(
        tool="invented_tool", confidence=0.9, arguments={}
    )

    decision = classify_tool_request("do something", registry, llm=mock_llm)

    assert decision.tool is None
    assert decision.confidence == 0.0
    assert decision.arguments == {}


def test_classify_tool_request_empty_registry():
    registry = SkillRegistry()

    decision = classify_tool_request("do something", registry, llm=MagicMock())

    # If registry is empty, it returns None immediately without calling LLM
    assert decision.tool is None
    assert decision.confidence == 0.0
