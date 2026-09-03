import pytest

from agent_os.default_registry import build_default_registry, parse_tier1_request
from agent_os.tools.bash import bash
from agent_os.tools.edit_file import edit_file
from agent_os.tools.read_file import read_file


def test_build_default_registry():
    registry = build_default_registry()
    names = registry.names()
    assert set(names) == {"read_file", "write_file", "bash", "memory_write"}

    assert registry.get("read_file").handler == read_file
    assert registry.get("write_file").handler == edit_file
    assert registry.get("bash").handler == bash


def test_parse_tier1_memory_write():
    registry = build_default_registry()

    decision = parse_tier1_request(
        "memory_write AI/Decisions/launch.md :: Launch approved",
        registry,
    )
    assert decision is not None
    assert decision.tool == "memory_write"
    assert decision.arguments == {
        "ref": "AI/Decisions/launch.md",
        "content": "Launch approved",
        "mode": "create",
    }

    append = parse_tier1_request(
        "memory-write append AI/Decisions/launch.md :: Add context",
        registry,
    )
    assert append is not None
    assert append.arguments["mode"] == "append"

    with pytest.raises(ValueError, match="missing '::' separator"):
        parse_tier1_request("memory_write AI/Decisions/launch.md", registry)


def test_parse_tier1_read():
    registry = build_default_registry()

    # Correct parsing
    decision = parse_tier1_request("read path/to/file.txt", registry)
    assert decision is not None
    assert decision.tool == "read_file"
    assert decision.confidence == 1.0
    assert decision.arguments == {"path": "path/to/file.txt"}

    # Missing args
    with pytest.raises(ValueError, match="Missing arguments"):
        parse_tier1_request("read", registry)

    with pytest.raises(ValueError, match="Missing arguments"):
        parse_tier1_request("read   ", registry)


def test_parse_tier1_write():
    registry = build_default_registry()

    # Correct parsing
    decision = parse_tier1_request("write path/to/file.txt :: Hello World", registry)
    assert decision is not None
    assert decision.tool == "write_file"
    assert decision.confidence == 1.0
    assert decision.arguments == {"path": "path/to/file.txt", "content": "Hello World"}

    # edit alias
    decision = parse_tier1_request("edit f.txt :: content", registry)
    assert decision.tool == "write_file"

    # Malformed separator
    with pytest.raises(ValueError, match="missing '::' separator"):
        parse_tier1_request("write file.txt content", registry)

    # Missing path
    with pytest.raises(ValueError, match="Missing path"):
        parse_tier1_request("write :: content", registry)

    # Missing content
    with pytest.raises(ValueError, match="Missing content"):
        parse_tier1_request("write file.txt :: ", registry)


def test_parse_tier1_bash():
    registry = build_default_registry()

    # Correct parsing
    decision = parse_tier1_request("bash echo 'Hello World'", registry)
    assert decision is not None
    assert decision.tool == "bash"
    assert decision.confidence == 1.0
    assert decision.arguments == {"cmd_args": ["echo", "Hello World"]}

    # Malformed quote
    with pytest.raises(ValueError, match="Malformed bash command"):
        parse_tier1_request("bash echo 'unclosed string", registry)

    # Empty args
    with pytest.raises(ValueError, match="Missing arguments"):
        parse_tier1_request("bash", registry)


def test_parse_tier1_no_match():
    registry = build_default_registry()

    # "please read this" -> "please" is the intent word. It should return None.
    decision = parse_tier1_request("please read this", registry)
    assert decision is None

    decision = parse_tier1_request("research something", registry)
    assert decision is None
