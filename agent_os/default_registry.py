import inspect
import logging
import shlex
from collections.abc import Sequence

from langchain_core.tools import BaseTool

from agent_os.connectors import WritableMemory
from agent_os.mcp import MCPClientFactory, MCPServerConfigs, load_mcp_tools
from agent_os.schemas import MEMORY_WRITE_MODES, RouterDecision
from agent_os.skills import RegisteredSkill, SkillRegistry
from agent_os.tools.bash import bash
from agent_os.tools.edit_file import edit_file
from agent_os.tools.memory_write import (
    build_memory_write_handler,
    default_memory_connector,
)
from agent_os.tools.read_file import read_file

logger = logging.getLogger(__name__)


def parse_tier1_request(
    text: str,
    registry: SkillRegistry,
) -> RouterDecision | None:
    """Parse an explicit native-tool command without invoking an LLM."""
    skill = registry.deterministic_match(text)
    if skill is None:
        return None

    words = text.strip().split(maxsplit=1)
    if len(words) < 2:
        raise ValueError(f"Missing arguments for command '{skill.name}'")
    arguments_text = words[1].strip()

    if skill.name == "read_file":
        return RouterDecision(
            tool="read_file",
            confidence=1.0,
            arguments={"path": arguments_text},
        )

    if skill.name == "write_file":
        if "::" not in arguments_text:
            raise ValueError("Malformed write command: missing '::' separator")
        path, content = arguments_text.split("::", 1)
        path = path.strip()
        content = content.strip()
        if not path:
            raise ValueError("Missing path for write command")
        if not content:
            raise ValueError("Missing content for write command")
        return RouterDecision(
            tool="write_file",
            confidence=1.0,
            arguments={"path": path, "content": content},
        )

    if skill.name == "bash":
        try:
            command_arguments = shlex.split(arguments_text)
        except ValueError as error:
            raise ValueError(f"Malformed bash command: {error}") from error
        if not command_arguments:
            raise ValueError("Empty bash command")
        return RouterDecision(
            tool="bash",
            confidence=1.0,
            arguments={"cmd_args": command_arguments},
        )

    if skill.name == "memory_write":
        if "::" not in arguments_text:
            raise ValueError("Malformed memory write command: missing '::' separator")
        destination, content = arguments_text.split("::", 1)
        destination_parts = destination.strip().split()
        content = content.strip()
        if not content:
            raise ValueError("Missing content for memory write command")
        if len(destination_parts) == 1:
            mode = "create"
            ref = destination_parts[0]
        elif len(destination_parts) == 2 and destination_parts[0] in MEMORY_WRITE_MODES:
            mode, ref = destination_parts
        else:
            modes = "|".join(sorted(MEMORY_WRITE_MODES))
            raise ValueError(
                "Malformed memory write command. Use "
                f"memory_write [{{{modes}}}] <ref> :: <content>"
            )
        if not ref:
            raise ValueError("Missing ref for memory write command")
        return RouterDecision(
            tool="memory_write",
            confidence=1.0,
            arguments={"ref": ref, "content": content, "mode": mode},
        )

    argument_name = _single_text_argument_name(skill)
    if argument_name is None:
        return None
    return RouterDecision(
        tool=skill.name,
        confidence=1.0,
        arguments={argument_name: arguments_text},
    )


def _single_text_argument_name(skill: RegisteredSkill) -> str | None:
    if isinstance(skill.handler, BaseTool):
        properties = (
            skill.handler.get_input_schema().model_json_schema().get("properties", {})
        )
        return next(iter(properties)) if len(properties) == 1 else None

    parameters = [
        parameter
        for parameter in inspect.signature(skill.handler).parameters.values()
        if parameter.kind
        not in {inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD}
    ]
    return parameters[0].name if len(parameters) == 1 else None


def build_default_registry(
    mcp_tools: Sequence[BaseTool] = (),
    memory_connector: WritableMemory | None = None,
) -> SkillRegistry:
    registry = SkillRegistry()
    registry.register(
        RegisteredSkill(name="read_file", aliases=["read"], handler=read_file)
    )
    registry.register(
        RegisteredSkill(
            name="write_file",
            aliases=["write", "edit"],
            handler=edit_file,
        )
    )
    registry.register(RegisteredSkill(name="bash", aliases=[], handler=bash))
    registry.register(
        RegisteredSkill(
            name="memory_write",
            aliases=["memory-write"],
            handler=build_memory_write_handler(
                memory_connector or default_memory_connector()
            ),
        )
    )

    for tool in mcp_tools:
        try:
            registry.register(RegisteredSkill(name=tool.name, aliases=[], handler=tool))
        except ValueError as error:
            logger.warning(
                "Skipping MCP tool %r due to registry collision: %s",
                tool.name,
                error,
            )

    return registry


async def build_default_registry_with_mcp(
    server_configs: MCPServerConfigs | None = None,
    client_factory: MCPClientFactory | None = None,
) -> SkillRegistry:
    """Build the native registry and add tools from enabled MCP servers."""
    mcp_tools = await load_mcp_tools(
        server_configs=server_configs,
        client_factory=client_factory,
    )
    return build_default_registry(mcp_tools=mcp_tools)
