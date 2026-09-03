import inspect
import json
import types
import typing
from collections.abc import Callable
from pathlib import Path

from agent_os import api

FIXTURE = Path(__file__).parent / "fixtures" / "public_api_v2.json"


def _annotation(value: object) -> str:
    if value is inspect.Signature.empty:
        return "unannotated"
    aliases = (
        (api.BackendRole, "BackendRole"),
        (api.BackendArtifact, "BackendArtifact"),
        (api.BackendInvoker, "BackendInvoker"),
        (api.SkillHandler, "SkillHandler"),
    )
    for candidate, name in aliases:
        if value is candidate:
            return name
    if value is None or value is types.NoneType:
        return "None"
    if value is typing.Any:
        return "Any"
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "[" + ", ".join(_annotation(item) for item in value) + "]"
    if isinstance(value, type):
        if value.__module__ == "builtins":
            return value.__name__
        if value.__module__.startswith("agent_os") and value.__name__ in api.__all__:
            return value.__name__
        return f"{value.__module__}.{value.__qualname__}"
    origin = typing.get_origin(value)
    args = typing.get_args(value)
    if origin in (typing.Union, types.UnionType):
        return " | ".join(_annotation(item) for item in args)
    if origin is typing.Literal:
        return "Literal[" + ", ".join(repr(item) for item in args) + "]"
    if origin in (typing.Callable, Callable):
        return "Callable[" + ", ".join(_annotation(item) for item in args) + "]"
    if origin is not None:
        name = getattr(origin, "__name__", str(origin).replace("typing.", ""))
        return f"{name}[{', '.join(_annotation(item) for item in args)}]"
    return str(value).replace("typing.", "")


def _default(value: object) -> object:
    if value is inspect.Signature.empty:
        return "required"
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return repr(value)


def _signature(target: object) -> str:
    signature = inspect.signature(target)
    parameters = []
    for parameter in signature.parameters.values():
        default = _default(parameter.default)
        rendered = f"{parameter.name}: {_annotation(parameter.annotation)}"
        if default != "required":
            rendered += f" = {default!r}"
        parameters.append(f"{parameter.kind.name} {rendered}")
    return f"({'; '.join(parameters)}) -> {_annotation(signature.return_annotation)}"


def build_public_contract() -> dict[str, object]:
    callable_targets = {
        "BackendAdapter.authentication_status": api.BackendAdapter.authentication_status,
        "BackendAdapter.build_invoker": api.BackendAdapter.build_invoker,
        "BackendRegistry.items": api.BackendRegistry.items,
        "BackendRegistry.register": api.BackendRegistry.register,
        "BackendRegistry.resolve": api.BackendRegistry.resolve,
        "Connector.capabilities": api.Connector.capabilities,
        "Connector.describe_side_effect": api.Connector.describe_side_effect,
        "Connector.invoke": api.Connector.invoke,
        "ConnectorRegistry.list_connectors": api.ConnectorRegistry.list_connectors,
        "ConnectorRegistry.register": api.ConnectorRegistry.register,
        "ConnectorRegistry.resolve": api.ConnectorRegistry.resolve,
        "ContextProvider.provide": api.ContextProvider.provide,
        "EventSink.emit": api.EventSink.emit,
        "IndexableMemory.index": api.IndexableMemory.index,
        "IndexableMemory.index_status": api.IndexableMemory.index_status,
        "IndexableMemory.reindex": api.IndexableMemory.reindex,
        "LocalPolicy.__init__": api.LocalPolicy.__init__,
        "LocalPolicy.evaluate": api.LocalPolicy.evaluate,
        "MemoryConnector.list_notes": api.MemoryConnector.list_notes,
        "MemoryConnector.read_note": api.MemoryConnector.read_note,
        "MemoryConnector.search": api.MemoryConnector.search,
        "MemoryWriteResult.__init__": api.MemoryWriteResult.__init__,
        "PluginRegistry.__init__": api.PluginRegistry.__init__,
        "PluginRegistry.discover": api.PluginRegistry.discover,
        "PluginRegistry.list_available": api.PluginRegistry.list_available,
        "PluginRegistry.resolve": api.PluginRegistry.resolve,
        "PolicyEngine.evaluate": api.PolicyEngine.evaluate,
        "Principal.__init__": api.Principal.__init__,
        "PrincipalResolver.resolve": api.PrincipalResolver.resolve,
        "RegisteredSkill.__init__": api.RegisteredSkill.__init__,
        "RegisteredSkill.invoke": api.RegisteredSkill.invoke,
        "RegisteredSkill.matches": api.RegisteredSkill.matches,
        "RegisteredSkill.router_description": api.RegisteredSkill.router_description,
        "SkillPackageLoader.__init__": api.SkillPackageLoader.__init__,
        "SkillPackageLoader.load_from_directories": api.SkillPackageLoader.load_from_directories,
        "SkillPackageLoader.load_from_entry_points": api.SkillPackageLoader.load_from_entry_points,
        "SkillPackageLoader.load_package": api.SkillPackageLoader.load_package,
        "SkillRegistry.deterministic_match": api.SkillRegistry.deterministic_match,
        "SkillRegistry.get": api.SkillRegistry.get,
        "SkillRegistry.names": api.SkillRegistry.names,
        "SkillRegistry.register": api.SkillRegistry.register,
        "SkillRegistry.router_catalog": api.SkillRegistry.router_catalog,
        "WritableMemory.describe_write_side_effect": api.WritableMemory.describe_write_side_effect,
        "WritableMemory.write_note": api.WritableMemory.write_note,
        "apply_policy": api.apply_policy,
    }
    protocol_attributes = {
        "BackendAdapter": {
            name: _annotation(annotation)
            for name, annotation in api.BackendAdapter.__annotations__.items()
        },
        "Connector": {"name": "str"},
        "ContextProvider": {"name": "str"},
        "EventSink": {"name": "str"},
        "IndexableMemory": {"name": "str"},
        "MemoryConnector": {"name": "str"},
        "PolicyEngine": {},
        "PrincipalResolver": {},
        "WritableMemory": {
            "name": "str",
            "supported_write_modes": "frozenset[str]",
        },
    }
    models = {}
    for model in (
        api.ActionProposal,
        api.AuthStatus,
        api.ContextBlock,
        api.ExecutionResult,
        api.IndexResult,
        api.MemoryHit,
        api.PolicyDecision,
    ):
        models[model.__name__] = {
            name: {
                "annotation": _annotation(field.annotation),
                "required": field.is_required(),
            }
            for name, field in model.model_fields.items()
        }
    return {
        "exports": list(api.__all__),
        "protocol_attributes": protocol_attributes,
        "callables": {
            name: _signature(target)
            for name, target in sorted(callable_targets.items())
        },
        "models": models,
    }


def test_public_contract_matches_reviewed_fixture() -> None:
    expected = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert build_public_contract() == expected


def test_behavior_through_public_facade(tmp_path: Path) -> None:
    class Memory:
        name = "memory"

        def search(self, query, limit=10):
            return [{"query": query}][:limit]

        def read_note(self, slug_or_path):
            return {"path": slug_or_path}

        def list_notes(self, filters=None):
            return []

    class Backend:
        name = "backend"
        binary_name = "backend"
        supported_roles = frozenset({"executor"})
        stub = False

        def build_invoker(self, role):
            return lambda state: api.ExecutionResult(status="completed")

        def authentication_status(self):
            return api.AuthStatus(status="ok")

    class Policy:
        def evaluate(self, proposal, *, workspace=None, context=None):
            return api.PolicyDecision(decision="allow", policy_id="test")

    connectors = api.ConnectorRegistry()
    connectors.register("memory", Memory())
    assert connectors.resolve("memory").search("needle") == [{"query": "needle"}]

    backends = api.BackendRegistry()
    backends.register(Backend())
    assert backends.resolve("executor", "backend").name == "backend"

    proposal = api.ActionProposal(tool="noop")
    result = api.apply_policy(
        Policy(),
        proposal,
        execute_fn=lambda action: api.ExecutionResult(status="completed"),
    )
    assert result.status == "completed"

    package = tmp_path / "skill"
    package.mkdir()
    (package / "manifest.toml").write_text(
        "[skill]\nname='echo'\nversion='1'\n"
        "[[skill.handlers]]\nmatch=['echo']\nentrypoint='handlers:echo'\n",
        encoding="utf-8",
    )
    (package / "handlers.py").write_text(
        "def echo(value):\n    return value\n", encoding="utf-8"
    )
    skills = api.SkillRegistry()
    api.SkillPackageLoader(skills).load_package(package)
    assert skills.get("echo").invoke({"value": "stable"}) == "stable"
