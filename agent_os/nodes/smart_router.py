from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage

from agent_os.llm import get_router_llm
from agent_os.schemas import RouterDecision
from agent_os.skills import SkillRegistry

ROUTER_PROMPT = """You are a routing agent. Classify the user's intent into one registered tool.

Available tools and input schemas:
{tool_list}

Examples:
- "read X" / "cat X" → tool: read_file, arguments: {{"path": "X"}}
- "write X :: content" → tool: write_file, arguments: {{"path": "X", "content": "content"}}
- "create X with Y" → tool: write_file (only if you can extract the exact path and complete file content)
- "add docstring to X" / "add type hints to X" / "refactor X" → tool: null, arguments: {{}} (Semantic coding work must escalate)

Rules:
1. Select only a listed tool and extract arguments matching its schema.
2. Return confidence from 0.0 to 1.0.
3. If no tool is suitable, if it requires semantic coding/reasoning, or if you cannot extract complete required arguments, set tool to null.
4. Never invent tools or execute argument strings.
5. Never select a tool when required arguments are missing.
"""


def classify_tool_request(
    task: str,
    registry: SkillRegistry,
    llm: BaseChatModel | None = None,
) -> RouterDecision:
    """Classify a task using the configured cheap structured-output model."""
    tool_names = registry.names()
    if not tool_names:
        return RouterDecision(tool=None, confidence=0.0, arguments={})

    resolved_llm = llm or get_router_llm()
    system_prompt = ROUTER_PROMPT.format(tool_list="\n".join(registry.router_catalog()))
    structured_llm = resolved_llm.with_structured_output(RouterDecision)
    decision = structured_llm.invoke(
        [
            SystemMessage(content=system_prompt),
            HumanMessage(content=task),
        ]
    )
    if not isinstance(decision, RouterDecision):
        raise ValueError("Router LLM failed to return a valid RouterDecision")
    if decision.tool is not None and decision.tool not in tool_names:
        return RouterDecision(tool=None, confidence=0.0, arguments={})
    return decision
