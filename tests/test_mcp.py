import logging
import os
from pathlib import Path
from unittest import mock

import pytest
from langchain_core.tools import tool

from agent_os.default_registry import (
    build_default_registry,
    build_default_registry_with_mcp,
)
from agent_os.mcp import build_mcp_server_configs, load_mcp_tools, parse_env_bool


def test_parse_env_bool_default():
    with mock.patch.dict(os.environ, clear=True):
        assert parse_env_bool("SOME_VAR", default=True) is True
        assert parse_env_bool("SOME_VAR", default=False) is False


@pytest.mark.parametrize(
    "val,expected",
    [
        ("1", True),
        ("true", True),
        ("YES", True),
        ("On", True),
        ("0", False),
        ("FALSE", False),
        ("no", False),
        ("oFF", False),
    ],
)
def test_parse_env_bool_valid(val, expected):
    with mock.patch.dict(os.environ, {"TEST_VAR": val}):
        assert parse_env_bool("TEST_VAR") is expected


def test_parse_env_bool_invalid():
    with mock.patch.dict(os.environ, {"TEST_VAR": "invalid"}):
        with pytest.raises(ValueError, match="Invalid boolean value"):
            parse_env_bool("TEST_VAR")


def test_build_mcp_server_configs_all_disabled():
    with mock.patch.dict(os.environ, clear=True):
        configs = build_mcp_server_configs()
        assert configs == {}


@pytest.fixture
def mock_home(tmp_path, monkeypatch):
    home_dir = tmp_path / "mock_home"
    home_dir.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home_dir)
    return home_dir


def test_build_mcp_server_configs_filesystem_valid_sandbox(mock_home):
    sandbox = mock_home / "sandbox"
    sandbox.mkdir()
    with mock.patch.dict(
        os.environ,
        {
            "MCP_FILESYSTEM_ENABLED": "true",
            "AGENT_OS_SANDBOX": str(sandbox),
        },
        clear=True,
    ):
        configs = build_mcp_server_configs()
        assert "filesystem" in configs
        fs_config = configs["filesystem"]
        assert fs_config["transport"] == "stdio"
        assert fs_config["command"] == "npx"
        assert fs_config["args"] == [
            "-y",
            "@modelcontextprotocol/server-filesystem",
            str(sandbox.resolve()),
        ]


def test_build_mcp_server_configs_filesystem_invalid_sandbox(
    mock_home,
    tmp_path,
):
    with mock.patch.dict(
        os.environ,
        {"MCP_FILESYSTEM_ENABLED": "true"},
        clear=True,
    ):
        with pytest.raises(ValueError, match="must be configured"):
            build_mcp_server_configs()

    with mock.patch.dict(
        os.environ,
        {
            "MCP_FILESYSTEM_ENABLED": "true",
            "AGENT_OS_SANDBOX": str(mock_home),
        },
        clear=True,
    ):
        with pytest.raises(
            ValueError, match="cannot be the user home directory itself"
        ):
            build_mcp_server_configs()

    for invalid_path in ["/", "/etc", "/var", "/System"]:
        with mock.patch.dict(
            os.environ,
            {"MCP_FILESYSTEM_ENABLED": "true", "AGENT_OS_SANDBOX": invalid_path},
            clear=True,
        ):
            with pytest.raises(
                ValueError, match="must be inside the user home directory"
            ):
                build_mcp_server_configs()

    outside_dir = tmp_path / "outside_home"
    outside_dir.mkdir()
    with mock.patch.dict(
        os.environ,
        {"MCP_FILESYSTEM_ENABLED": "true", "AGENT_OS_SANDBOX": str(outside_dir)},
        clear=True,
    ):
        with pytest.raises(ValueError, match="must be inside the user home directory"):
            build_mcp_server_configs()

    escape_link = mock_home / "escape"
    escape_link.symlink_to(outside_dir)
    with mock.patch.dict(
        os.environ,
        {"MCP_FILESYSTEM_ENABLED": "true", "AGENT_OS_SANDBOX": str(escape_link)},
        clear=True,
    ):
        with pytest.raises(ValueError, match="must be inside the user home directory"):
            build_mcp_server_configs()


def test_build_mcp_server_configs_codegraph():
    with mock.patch.dict(
        os.environ,
        {"MCP_CODEGRAPH_ENABLED": "true"},
        clear=True,
    ):
        configs = build_mcp_server_configs()
        assert "codegraph" in configs
        assert configs["codegraph"]["transport"] == "stdio"
        assert configs["codegraph"]["command"] == "codegraph"
        assert configs["codegraph"]["args"] == ["serve", "--mcp"]

    with mock.patch.dict(
        os.environ,
        {
            "MCP_CODEGRAPH_ENABLED": "true",
            "MCP_CODEGRAPH_COMMAND": "/path/to/custom_codegraph ",
        },
        clear=True,
    ):
        configs = build_mcp_server_configs()
        assert configs["codegraph"]["command"] == "/path/to/custom_codegraph"

    with mock.patch.dict(
        os.environ,
        {"MCP_CODEGRAPH_ENABLED": "true", "MCP_CODEGRAPH_COMMAND": "   "},
        clear=True,
    ):
        with pytest.raises(ValueError, match="cannot be blank when enabled"):
            build_mcp_server_configs()


def test_build_mcp_server_configs_gbrain():
    with mock.patch.dict(
        os.environ,
        {"MCP_GBRAIN_URL": "http://gbrain.local"},
        clear=True,
    ):
        configs = build_mcp_server_configs()
        assert "gbrain" in configs
        assert configs["gbrain"]["transport"] == "http"
        assert configs["gbrain"]["url"] == "http://gbrain.local"


@pytest.mark.parametrize(
    "url",
    [
        "gbrain.local/mcp",
        "ftp://gbrain.local/mcp",
        "https://user:secret@gbrain.local/mcp",
    ],
)
def test_build_mcp_server_configs_rejects_unsafe_gbrain_url(url):
    with mock.patch.dict(os.environ, {"MCP_GBRAIN_URL": url}, clear=True):
        with pytest.raises(ValueError, match="MCP_GBRAIN_URL"):
            build_mcp_server_configs()


def test_build_mcp_server_configs_combined(mock_home):
    sandbox = mock_home / "sandbox"
    sandbox.mkdir()
    with mock.patch.dict(
        os.environ,
        {
            "MCP_FILESYSTEM_ENABLED": "true",
            "AGENT_OS_SANDBOX": str(sandbox),
            "MCP_CODEGRAPH_ENABLED": "true",
            "MCP_GBRAIN_URL": "http://gbrain.local",
        },
        clear=True,
    ):
        configs = build_mcp_server_configs()
        assert len(configs) == 3
        assert "filesystem" in configs
        assert "codegraph" in configs
        assert "gbrain" in configs


class FakeClient:
    def __init__(self, configs):
        self.configs = configs

    async def get_tools(self):
        name = list(self.configs.keys())[0]

        if name == "fail_server":
            raise RuntimeError("Connection refused with token=do-not-log")

        @tool
        def dummy_tool(x: int) -> int:
            """Dummy description"""
            return x

        dummy_tool.name = f"tool_from_{name}"
        return [dummy_tool]


@pytest.mark.anyio
async def test_load_mcp_tools_empty():
    client_factory = mock.Mock()
    tools = await load_mcp_tools({}, client_factory=client_factory)
    assert tools == []
    client_factory.assert_not_called()


@pytest.mark.anyio
async def test_load_mcp_tools_successful():
    configs = {"server1": {"transport": "stdio"}}
    tools = await load_mcp_tools(configs, client_factory=FakeClient)
    assert len(tools) == 1
    loaded_tool = tools[0]
    assert loaded_tool.name == "mcp_server1_tool_from_server1"
    assert loaded_tool.description == "Dummy description"
    assert "x" in loaded_tool.args_schema.model_fields
    assert loaded_tool.invoke({"x": 7}) == 7


@pytest.mark.anyio
async def test_load_mcp_tools_does_not_mutate_source_tool():
    @tool
    def source_tool(value: str) -> str:
        """Return the source value."""
        return value

    class StaticClient:
        def __init__(self, configs):
            self.configs = configs

        async def get_tools(self):
            return [source_tool]

    tools = await load_mcp_tools(
        {"server": {"transport": "stdio"}},
        client_factory=StaticClient,
    )

    assert source_tool.name == "source_tool"
    assert tools[0].name == "mcp_server_source_tool"
    assert tools[0].invoke({"value": "preserved"}) == "preserved"


@pytest.mark.anyio
async def test_load_mcp_tools_multiple_servers():
    configs = {
        "s1": {"transport": "stdio"},
        "s2": {"transport": "stdio"},
    }
    tools = await load_mcp_tools(configs, client_factory=FakeClient)
    assert len(tools) == 2
    names = {t.name for t in tools}
    assert names == {"mcp_s1_tool_from_s1", "mcp_s2_tool_from_s2"}


@pytest.mark.anyio
async def test_load_mcp_tools_partial_failure(caplog):
    configs = {
        "s1": {"transport": "stdio"},
        "fail_server": {"transport": "stdio"},
    }
    with caplog.at_level(logging.WARNING):
        tools = await load_mcp_tools(configs, client_factory=FakeClient)

    assert len(tools) == 1
    assert tools[0].name == "mcp_s1_tool_from_s1"
    assert "Failed to load MCP tools from server 'fail_server'" in caplog.text
    assert "RuntimeError" in caplog.text
    assert "do-not-log" not in caplog.text


@pytest.mark.anyio
async def test_load_mcp_tools_all_fail():
    configs = {"fail_server": {"transport": "stdio"}}
    tools = await load_mcp_tools(configs, client_factory=FakeClient)
    assert tools == []


@pytest.mark.anyio
async def test_load_mcp_tools_skips_malformed_tool(caplog):
    class MalformedClient:
        def __init__(self, configs):
            self.configs = configs

        async def get_tools(self):
            return [object()]

    with caplog.at_level(logging.WARNING):
        tools = await load_mcp_tools(
            {"bad": {"transport": "stdio"}},
            client_factory=MalformedClient,
        )

    assert tools == []
    assert "Skipping invalid MCP tool from server 'bad' (object)" in caplog.text


@pytest.mark.anyio
@pytest.mark.parametrize("returned_tools", [[], None])
async def test_load_mcp_tools_handles_empty_or_malformed_collection(
    returned_tools,
    caplog,
):
    class CollectionClient:
        def __init__(self, configs):
            self.configs = configs

        async def get_tools(self):
            return returned_tools

    with caplog.at_level(logging.WARNING):
        tools = await load_mcp_tools(
            {"collection": {"transport": "stdio"}},
            client_factory=CollectionClient,
        )

    assert tools == []
    if returned_tools is None:
        assert "invalid MCP tool collection" in caplog.text
    else:
        assert "invalid MCP tool collection" not in caplog.text


def test_build_default_registry_native_only():
    registry = build_default_registry()
    assert registry.get("read_file") is not None
    assert registry.get("write_file") is not None
    assert registry.get("bash") is not None
    assert registry.get("memory_write") is not None
    assert set(registry.names()) == {"read_file", "write_file", "bash", "memory_write"}


def test_build_default_registry_with_mcp_tools():
    @tool
    def my_mcp_tool(x: str) -> str:
        """doc"""
        return x + "!"

    my_mcp_tool.name = "mcp_s1_my_mcp_tool"
    registry = build_default_registry(mcp_tools=[my_mcp_tool])

    assert len(registry.names()) == 5
    assert registry.get("memory_write") is not None
    mcp_skill = registry.get("mcp_s1_my_mcp_tool")
    assert mcp_skill is not None
    assert mcp_skill.invoke({"x": "hello"}) == "hello!"


def test_build_default_registry_collision(caplog):
    @tool
    def duplicate_tool():
        """doc"""
        return "ok"

    duplicate_tool.name = "read_file"

    with caplog.at_level(logging.WARNING):
        registry = build_default_registry(mcp_tools=[duplicate_tool])

    assert len(registry.names()) == 4
    assert registry.get("memory_write") is not None
    assert "Skipping MCP tool 'read_file' due to registry collision" in caplog.text


@pytest.mark.anyio
async def test_build_default_registry_with_mcp_async():
    configs = {"s1": {"transport": "stdio"}}
    registry = await build_default_registry_with_mcp(
        server_configs=configs,
        client_factory=FakeClient,
    )

    assert len(registry.names()) == 5
    assert registry.get("mcp_s1_tool_from_s1") is not None
    assert registry.get("read_file") is not None
    assert registry.get("memory_write") is not None
