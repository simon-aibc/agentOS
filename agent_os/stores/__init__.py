"""Storage protocols and reference SQLite implementations for Agent OS (Phase 0).

Provides storage seams for run ledgers, schedules, permission rules, and observations,
with dynamic entry-point discovery via the 'agent_os.stores' group.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from agent_os.plugins import PluginRegistry
from agent_os.stores.base import configure_sqlite_connection
from agent_os.stores.protocols import (
    ObservationStore,
    PermissionStore,
    RunStore,
    ScheduleStore,
)

if TYPE_CHECKING:
    from agent_os.observations import SqliteObservationStore
    from agent_os.permission_store import (
        InMemoryPermissionStore,
        PermissionRuleStore,
        SqlitePermissionStore,
    )
    from agent_os.stores.sqlite_runs import SqliteRunStore
    from agent_os.stores.sqlite_schedules import SqliteScheduleStore

PermissionStoreProtocol = PermissionStore

__all__ = (
    "InMemoryPermissionStore",
    "ObservationStore",
    "PermissionRuleStore",
    "PermissionStore",
    "PermissionStoreProtocol",
    "RunStore",
    "ScheduleStore",
    "SqliteObservationStore",
    "SqlitePermissionStore",
    "SqliteRunStore",
    "SqliteScheduleStore",
    "configure_sqlite_connection",
    "list_store_backends",
    "resolve_store_backend",
)


def resolve_store_backend(name: str) -> Any:
    """Resolve a pluggable store backend class or factory from the 'agent_os.stores' entry point group."""
    registry = PluginRegistry()
    return registry.resolve("agent_os.stores", name)


def list_store_backends() -> list[str]:
    """List available store backend names registered under 'agent_os.stores'."""
    registry = PluginRegistry()
    return registry.list_available("agent_os.stores")


def __getattr__(name: str) -> Any:
    if name == "SqliteRunStore":
        from agent_os.stores.sqlite_runs import SqliteRunStore

        return SqliteRunStore
    if name == "SqliteScheduleStore":
        from agent_os.stores.sqlite_schedules import SqliteScheduleStore

        return SqliteScheduleStore
    if name == "SqliteObservationStore":
        from agent_os.observations import SqliteObservationStore

        return SqliteObservationStore
    if name in ("SqlitePermissionStore", "InMemoryPermissionStore", "PermissionRuleStore"):
        import agent_os.permission_store as perm

        return getattr(perm, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
