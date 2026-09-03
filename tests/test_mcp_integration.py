import os
import shutil
from pathlib import Path
from unittest import mock

import anyio
import pytest

from agent_os.mcp import load_mcp_tools, parse_env_bool

pytestmark = pytest.mark.integration


@pytest.mark.anyio
async def test_mcp_filesystem_integration(tmp_path):
    if not parse_env_bool("MCP_FILESYSTEM_ENABLED"):
        pytest.skip("MCP_FILESYSTEM_ENABLED is false or unset")
    if not shutil.which("npx"):
        pytest.skip("npx not found in PATH, required for filesystem server")

    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()

    with (
        mock.patch("agent_os.mcp.Path.home", return_value=tmp_path),
        mock.patch.dict(os.environ, {"AGENT_OS_SANDBOX": str(sandbox)}),
    ):
        with anyio.fail_after(15):
            tools = await load_mcp_tools()

    filesystem_tools = [
        tool for tool in tools if tool.name.startswith("mcp_filesystem_")
    ]
    assert filesystem_tools, "No filesystem tools were loaded"


@pytest.mark.anyio
async def test_mcp_codegraph_integration():
    if not parse_env_bool("MCP_CODEGRAPH_ENABLED"):
        pytest.skip("MCP_CODEGRAPH_ENABLED is false or unset")
    if not shutil.which("codegraph"):
        pytest.skip("codegraph binary not found in PATH")
    if not (Path.cwd() / ".codegraph").is_dir():
        pytest.skip(".codegraph index directory not found at project root")

    with anyio.fail_after(15):
        tools = await load_mcp_tools()

    codegraph_tools = [tool for tool in tools if tool.name.startswith("mcp_codegraph_")]
    assert codegraph_tools, "No CodeGraph tools were loaded"


@pytest.mark.anyio
async def test_mcp_gbrain_integration():
    if not os.getenv("MCP_GBRAIN_URL"):
        pytest.skip("MCP_GBRAIN_URL is missing or empty")

    with anyio.fail_after(15):
        tools = await load_mcp_tools()

    gbrain_tools = [tool for tool in tools if tool.name.startswith("mcp_gbrain_")]
    assert gbrain_tools, "No gbrain tools were loaded"
