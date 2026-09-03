import pytest

from agent_os.default_registry import parse_tier1_request
from agent_os.skills import RegisteredSkill, SkillRegistry


def test_registered_skill_matches():
    skill = RegisteredSkill(name="research", aliases=["find"], handler=lambda: None)

    assert skill.matches("research something") is True
    assert skill.matches("find something") is True
    # no "read" match inside "research" or "readme"
    assert skill.matches("read something") is False
    assert skill.matches("readme something") is False


def test_registry_deterministic_match():
    registry = SkillRegistry()
    registry.register(
        RegisteredSkill(name="read", aliases=["cat", "view"], handler=lambda: None)
    )
    registry.register(
        RegisteredSkill(name="write", aliases=["edit"], handler=lambda: None)
    )

    # Exact intent matching
    assert registry.deterministic_match("read file.txt").name == "read"
    assert registry.deterministic_match("cat file.txt").name == "read"
    assert registry.deterministic_match("edit file.txt").name == "write"

    # Missing / empty
    assert registry.deterministic_match("") is None
    assert registry.deterministic_match("   ") is None

    # no match inside other words
    assert registry.deterministic_match("research file") is None
    assert registry.deterministic_match("readme file") is None


def test_registry_duplicate_rejection():
    registry = SkillRegistry()
    skill1 = RegisteredSkill(name="read", aliases=["cat"], handler=lambda: None)
    registry.register(skill1)

    # duplicate canonical name
    with pytest.raises(ValueError, match="already registered"):
        registry.register(
            RegisteredSkill(name="read", aliases=[], handler=lambda: None)
        )

    # duplicate canonical name as alias
    with pytest.raises(ValueError, match="already used as an alias"):
        registry.register(RegisteredSkill(name="cat", aliases=[], handler=lambda: None))

    # alias duplicates canonical name
    with pytest.raises(ValueError, match="already used by"):
        registry.register(
            RegisteredSkill(name="view", aliases=["read"], handler=lambda: None)
        )

    # alias duplicates alias
    with pytest.raises(ValueError, match="already used by"):
        registry.register(
            RegisteredSkill(name="view", aliases=["cat"], handler=lambda: None)
        )


def test_registry_runtime_extensibility():
    registry = SkillRegistry()
    assert registry.names() == []

    registry.register(RegisteredSkill(name="test", aliases=[], handler=lambda: None))
    assert registry.names() == ["test"]
    assert registry.get("test").name == "test"

    registry.register(RegisteredSkill(name="test2", aliases=[], handler=lambda: None))
    assert set(registry.names()) == {"test", "test2"}


def test_skill_names_and_aliases_are_normalized_and_immutable():
    aliases = ["View"]
    skill = RegisteredSkill(name=" Read_File ", aliases=aliases, handler=lambda: None)
    aliases.append("mutated")

    assert skill.name == "read_file"
    assert skill.aliases == ("view",)
    assert skill.matches("VIEW file.txt") is True


def test_skill_rejects_duplicate_or_empty_aliases():
    with pytest.raises(ValueError, match="canonical name"):
        RegisteredSkill(name="read", aliases=["READ"], handler=lambda: None)
    with pytest.raises(ValueError, match="unique"):
        RegisteredSkill(name="read", aliases=["cat", "CAT"], handler=lambda: None)
    with pytest.raises(ValueError, match="must not be empty"):
        RegisteredSkill(name="read", aliases=["  "], handler=lambda: None)


def test_tier1_request_routes_single_argument_workspace_skill_without_llm():
    registry = SkillRegistry()
    registry.register(
        RegisteredSkill(
            name="hermes_chat",
            aliases=["hermes-chat"],
            handler=lambda task: task,
        )
    )

    decision = parse_tier1_request('hermes-chat {"message":"hello"}', registry)

    assert decision is not None
    assert decision.tool == "hermes_chat"
    assert decision.confidence == 1.0
    assert decision.arguments == {"task": '{"message":"hello"}'}
