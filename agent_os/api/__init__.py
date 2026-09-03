"""Stable public extension API for Agent OS.

Only names exported here are covered by the v2 compatibility policy.
"""

from agent_os.backends import (
    AuthStatus,
    BackendAdapter,
    BackendArtifact,
    BackendInvoker,
    BackendRegistry,
    BackendRole,
)
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
from agent_os.policy import LocalPolicy, PolicyEngine, apply_policy
from agent_os.principal import Principal, PrincipalResolver
from agent_os.schemas import (
    SUPPORTED_SIDE_EFFECTS,
    ActionProposal,
    ExecutionResult,
    PolicyDecision,
)
from agent_os.skill_packages import SkillPackageLoader
from agent_os.skills import RegisteredSkill, SkillHandler, SkillRegistry

__all__ = (
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
)
