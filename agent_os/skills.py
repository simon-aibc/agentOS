import inspect
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from langchain_core.tools import BaseTool

SkillHandler = BaseTool | Callable[..., Any]


@dataclass(frozen=True)
class RegisteredSkill:
    """A native or injected skill registered with the dispatcher."""

    name: str
    aliases: Sequence[str]
    handler: SkillHandler

    def __post_init__(self) -> None:
        normalized_name = self.name.strip().lower()
        normalized_aliases = tuple(alias.strip().lower() for alias in self.aliases)
        if not normalized_name:
            raise ValueError("Skill name must not be empty")
        if any(not alias for alias in normalized_aliases):
            raise ValueError("Skill aliases must not be empty")
        if normalized_name in normalized_aliases:
            raise ValueError("Skill aliases must not duplicate the canonical name")
        if len(normalized_aliases) != len(set(normalized_aliases)):
            raise ValueError("Skill aliases must be unique")
        object.__setattr__(self, "name", normalized_name)
        object.__setattr__(self, "aliases", normalized_aliases)

    def matches(self, command: str) -> bool:
        words = command.strip().lower().split()
        if not words:
            return False
        first_word = words[0]
        return first_word == self.name or first_word in self.aliases

    def invoke(self, arguments: dict[str, object]) -> Any:
        """Invoke LangChain tools and plain callables through one contract."""
        if isinstance(self.handler, BaseTool):
            return self.handler.invoke(arguments)
        return self.handler(**arguments)

    def router_description(self) -> str:
        """Return a deterministic input schema for the structured router."""
        if isinstance(self.handler, BaseTool):
            schema = self.handler.get_input_schema().model_json_schema()
            return json.dumps(schema, sort_keys=True)
        return str(inspect.signature(self.handler))


class SkillRegistry:
    """An isolated, runtime-extensible registry for tools and skills."""

    def __init__(self) -> None:
        self._skills: dict[str, RegisteredSkill] = {}

    def register(self, skill: RegisteredSkill) -> None:
        """Register a skill while preserving unique canonical names and aliases."""
        if skill.name in self._skills:
            raise ValueError(f"Skill canonical name already registered: {skill.name}")

        for existing in self._skills.values():
            if skill.name in existing.aliases:
                raise ValueError(
                    f"Skill name '{skill.name}' is already used as an alias "
                    f"for '{existing.name}'"
                )
            for alias in skill.aliases:
                if alias == existing.name or alias in existing.aliases:
                    raise ValueError(
                        f"Alias '{alias}' already used by '{existing.name}'"
                    )

        self._skills[skill.name] = skill

    def register_many(self, skills: Sequence[RegisteredSkill]) -> None:
        """Validate a batch first, then register it without partial mutation.

        Package loading can discover several handlers.  A collision in a later
        handler must not leave the earlier handlers in the live registry.
        """
        shadow = SkillRegistry()
        for existing in self._skills.values():
            shadow.register(existing)
        for skill in skills:
            shadow.register(skill)
        for skill in skills:
            self._skills[skill.name] = skill

    def get(self, name: str) -> RegisteredSkill | None:
        """Retrieve a skill by canonical name."""
        return self._skills.get(name.strip().lower())

    def names(self) -> list[str]:
        """Return canonical skill names in registration order."""
        return list(self._skills)

    def deterministic_match(self, text: str) -> RegisteredSkill | None:
        """Match an exact canonical name or alias at the start of a task."""
        for skill in self._skills.values():
            if skill.matches(text):
                return skill
        return None

    def router_catalog(self) -> list[str]:
        """Describe registered tools and their accepted input schemas."""
        return [
            f"- {skill.name}: {skill.router_description()}"
            for skill in self._skills.values()
        ]
