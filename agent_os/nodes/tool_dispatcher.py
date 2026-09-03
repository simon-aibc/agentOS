import json
from collections.abc import Callable
from typing import Literal

from langchain_core.language_models.chat_models import BaseChatModel
from langgraph.errors import GraphBubbleUp
from langgraph.graph import END
from langgraph.types import Command
from pydantic import BaseModel

from agent_os.default_registry import build_default_registry, parse_tier1_request
from agent_os.nodes.smart_router import classify_tool_request
from agent_os.output_limits import DISPATCHER_OUTPUT_MAX_BYTES, truncate_utf8
from agent_os.routing import get_router_mode
from agent_os.schemas import BashResult, ExecutionResult, ToolExecutionResult
from agent_os.skills import RegisteredSkill, SkillRegistry
from agent_os.state import AgentState

ROUTER_CONFIDENCE_THRESHOLD = 0.80
DispatcherCommand = Command[Literal["supervisor", "__end__"]]


def _serialize_tool_output(raw_output: object) -> str:
    if isinstance(raw_output, BaseModel):
        serialized = raw_output.model_dump_json(indent=2)
    elif isinstance(raw_output, (dict, list, tuple)):
        serialized = json.dumps(raw_output, sort_keys=True, default=str)
    else:
        serialized = str(raw_output)
    capped_output, _ = truncate_utf8(serialized, DISPATCHER_OUTPUT_MAX_BYTES)
    return capped_output


def _escalate(
    tool: str | None = None,
    error: Exception | None = None,
) -> DispatcherCommand:
    update: dict[str, object] = {"router_escalated": True}
    if error is not None:
        update["tool_result"] = ToolExecutionResult(
            tool=tool or "router",
            output=str(error),
            success=False,
        )
    return Command(update=update, goto="supervisor")


def _invoke_skill(
    skill: RegisteredSkill,
    arguments: dict[str, object],
) -> DispatcherCommand:
    try:
        raw_output = skill.invoke(arguments)
    except GraphBubbleUp:
        # ``interrupt()`` uses this control-flow exception.  It must reach
        # LangGraph unchanged so a policy approval can pause and resume the
        # actual tool invocation rather than being misreported as routing
        # escalation.
        raise
    except Exception as error:
        return _escalate(skill.name, error)

    return Command(
        update={
            "tool_result": ToolExecutionResult(
                tool=skill.name,
                output=_serialize_tool_output(raw_output),
                success=(
                    raw_output.success
                    if isinstance(raw_output, ExecutionResult)
                    else not (
                        isinstance(raw_output, BashResult)
                        and raw_output.returncode not in (0, None)
                    )
                ),
            ),
            "router_escalated": False,
        },
        goto=END,
    )


def build_tool_dispatcher_node(
    registry: SkillRegistry | None = None,
    router_llm: BaseChatModel | None = None,
) -> Callable[[AgentState], DispatcherCommand]:
    """Build an injectable Tier-1/Tier-2/Tier-3 dispatcher node."""
    resolved_registry = registry or build_default_registry()

    def dispatch(state: AgentState) -> DispatcherCommand:
        task = state["task"]
        deterministic_skill = resolved_registry.deterministic_match(task)
        try:
            decision = parse_tier1_request(task, resolved_registry)
        except ValueError as error:
            return _escalate(
                deterministic_skill.name if deterministic_skill else None,
                error,
            )

        if decision is None:
            if get_router_mode() == "direct-escalation":
                return _escalate()
            try:
                decision = classify_tool_request(
                    task,
                    resolved_registry,
                    llm=router_llm,
                )
            except Exception as error:
                return _escalate(error=error)

        if decision.tool is None or decision.confidence < ROUTER_CONFIDENCE_THRESHOLD:
            return _escalate()

        skill = resolved_registry.get(decision.tool)
        if skill is None:
            return _escalate()
        return _invoke_skill(skill, decision.arguments)

    return dispatch


tool_dispatcher_node = build_tool_dispatcher_node()
