from pathlib import Path

from agent_os import api
from agent_os.backends import BackendAdapter, BackendRegistry
from agent_os.connectors import (
    Connector,
    ConnectorRegistry,
    IndexableMemory,
    IndexResult,
    MemoryConnector,
    MemoryHit,
    MemoryWriteResult,
    WritableMemory,
)
from agent_os.context import ContextBlock, ContextProvider
from agent_os.events import EventSink
from agent_os.plugins import PluginRegistry
from agent_os.policy import PolicyEngine
from agent_os.principal import Principal, PrincipalResolver
from agent_os.skill_packages import SkillPackageLoader

EXPECTED_EXPORTS = {
    "ActionProposal",
    "AuthStatus",
    "BackendAdapter",
    "BackendArtifact",
    "BackendInvoker",
    "BackendRegistry",
    "BackendRole",
    "Connector",
    "ConnectorRegistry",
    "ContextBlock",
    "ContextProvider",
    "EventSink",
    "ExecutionResult",
    "IndexResult",
    "IndexableMemory",
    "LocalPolicy",
    "MemoryConnector",
    "MemoryHit",
    "MemoryWriteResult",
    "PluginRegistry",
    "PolicyDecision",
    "PolicyEngine",
    "Principal",
    "PrincipalResolver",
    "RegisteredSkill",
    "SUPPORTED_SIDE_EFFECTS",
    "SkillHandler",
    "SkillPackageLoader",
    "SkillRegistry",
    "WritableMemory",
    "apply_policy",
}


def test_public_export_set_is_exact() -> None:
    assert set(api.__all__) == EXPECTED_EXPORTS
    assert len(api.__all__) == len(EXPECTED_EXPORTS)


def test_facade_reexports_runtime_objects_by_identity() -> None:
    assert api.MemoryConnector is MemoryConnector
    assert api.Connector is Connector
    assert api.WritableMemory is WritableMemory
    assert api.MemoryWriteResult is MemoryWriteResult
    assert api.MemoryHit is MemoryHit
    assert api.IndexResult is IndexResult
    assert api.IndexableMemory is IndexableMemory
    assert api.ContextBlock is ContextBlock
    assert api.ContextProvider is ContextProvider
    assert api.EventSink is EventSink
    assert api.PluginRegistry is PluginRegistry
    assert api.ConnectorRegistry is ConnectorRegistry
    assert api.BackendAdapter is BackendAdapter
    assert api.BackendRegistry is BackendRegistry
    assert api.PolicyEngine is PolicyEngine
    assert api.SkillPackageLoader is SkillPackageLoader
    assert api.Principal is Principal
    assert api.PrincipalResolver is PrincipalResolver


def test_protocols_accept_minimal_structural_implementations() -> None:
    class Memory:
        name = "memory"

        def search(self, query: str, limit: int = 10) -> list[dict]:
            return [{"query": query}][:limit]

        def read_note(self, slug_or_path: str) -> dict:
            return {"path": slug_or_path}

        def list_notes(self, filters: dict | None = None) -> list[dict]:
            return [filters or {}]

    class Backend:
        name = "dummy"
        binary_name = "dummy"
        supported_roles = frozenset({"executor"})
        stub = False

        def build_invoker(self, role):
            return lambda state: api.ExecutionResult(status="completed")

        def authentication_status(self):
            return api.AuthStatus(status="ok")

    class Policy:
        def evaluate(self, proposal, *, workspace=None, context=None):
            return api.PolicyDecision(
                decision="allow", policy_id="test", reason="allowed"
            )

    memory_registry = api.ConnectorRegistry()
    memory_registry.register("memory", Memory())
    assert memory_registry.resolve("memory").read_note("note") == {"path": "note"}

    backend_registry = api.BackendRegistry()
    backend_registry.register(Backend())
    assert backend_registry.resolve("executor", "dummy").name == "dummy"

    proposal = api.ActionProposal(tool="noop")
    assert Policy().evaluate(proposal).decision == "allow"


def test_distribution_declares_typing_marker() -> None:
    assert (Path(api.__path__[0]).parent / "py.typed").is_file()
