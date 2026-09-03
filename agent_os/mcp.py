import logging
import os
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit

from langchain_core.tools import BaseTool

MCPServerConfig = dict[str, object]
MCPServerConfigs = Mapping[str, MCPServerConfig]

logger = logging.getLogger(__name__)


class MCPClient(Protocol):
    """Small client contract used to keep MCP loading easy to test."""

    async def get_tools(self) -> list[BaseTool]: ...


MCPClientFactory = Callable[[dict[str, MCPServerConfig]], MCPClient]


def parse_env_bool(name: str, default: bool = False) -> bool:
    """Parse a conventional boolean environment variable."""
    value = os.getenv(name)
    if value is None:
        return default

    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"Invalid boolean value for {name}: {value!r}")


def _validate_http_url(name: str, value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError(f"{name} must be an absolute HTTP(S) URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError(f"{name} must not contain embedded credentials")
    return value


def _validate_sandbox_root(sandbox_path: str) -> Path:
    """Require the filesystem MCP root to be strictly below the user home."""
    if not sandbox_path:
        raise ValueError(
            "AGENT_OS_SANDBOX must be configured when MCP_FILESYSTEM_ENABLED is true"
        )
    resolved = Path(sandbox_path).resolve()
    home = Path.home().resolve()

    if resolved == home:
        raise ValueError(
            "Filesystem MCP root cannot be the user home directory itself."
        )

    try:
        resolved.relative_to(home)
    except ValueError:
        raise ValueError(
            "Filesystem MCP root must be inside the user home directory."
        ) from None

    return resolved


def build_mcp_server_configs() -> dict[str, MCPServerConfig]:
    """Build adapter-compatible server configurations from the environment."""
    configs: dict[str, MCPServerConfig] = {}

    if parse_env_bool("MCP_FILESYSTEM_ENABLED"):
        sandbox_path = os.getenv("AGENT_OS_SANDBOX", "")
        resolved_sandbox = _validate_sandbox_root(sandbox_path)

        configs["filesystem"] = {
            "transport": "stdio",
            "command": "npx",
            "args": [
                "-y",
                "@modelcontextprotocol/server-filesystem",
                str(resolved_sandbox),
            ],
        }

    if parse_env_bool("MCP_CODEGRAPH_ENABLED"):
        cg_command = os.getenv("MCP_CODEGRAPH_COMMAND", "codegraph").strip()
        if not cg_command:
            raise ValueError("MCP_CODEGRAPH_COMMAND cannot be blank when enabled.")

        configs["codegraph"] = {
            "transport": "stdio",
            "command": cg_command,
            "args": ["serve", "--mcp"],
        }

    gbrain_url = os.getenv("MCP_GBRAIN_URL", "").strip()
    if gbrain_url:
        configs["gbrain"] = {
            "transport": "http",
            "url": _validate_http_url("MCP_GBRAIN_URL", gbrain_url),
        }

    return configs


async def load_mcp_tools(
    server_configs: MCPServerConfigs | None = None,
    client_factory: MCPClientFactory | None = None,
) -> list[BaseTool]:
    """Load and prefix tools, isolating failures to their source server."""
    resolved_configs = (
        build_mcp_server_configs() if server_configs is None else server_configs
    )
    if not resolved_configs:
        return []

    if client_factory is None:
        from langchain_mcp_adapters.client import MultiServerMCPClient

        client_factory = MultiServerMCPClient

    all_tools: list[BaseTool] = []
    for server_name, config in resolved_configs.items():
        try:
            client = client_factory({server_name: config})
            server_tools = await client.get_tools()
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as error:
            logger.warning(
                "Failed to load MCP tools from server %r (%s)",
                server_name,
                type(error).__name__,
            )
            continue

        if not isinstance(server_tools, list):
            logger.warning(
                "Skipping invalid MCP tool collection from server %r (%s)",
                server_name,
                type(server_tools).__name__,
            )
            continue

        prefix = f"mcp_{server_name}_"
        for tool in server_tools:
            if not isinstance(tool, BaseTool):
                logger.warning(
                    "Skipping invalid MCP tool from server %r (%s)",
                    server_name,
                    type(tool).__name__,
                )
                continue
            prefixed_name = f"{prefix}{tool.name}"
            all_tools.append(tool.model_copy(update={"name": prefixed_name}))

    return all_tools
