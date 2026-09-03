"""Generic plugin registry and entry-point discovery for Agent OS (Phase 1).

Discovers extensions published under standard setuptools / importlib entry point groups
(e.g., 'agent_os.connectors', 'agent_os.memory_connectors').
"""

from __future__ import annotations

import importlib.metadata
from typing import Any


class PluginError(ValueError):
    """Base error for plugin discovery and loading failures."""


class PluginDiscoveryError(PluginError):
    """Raised when duplicate or conflicting entry points are discovered in a group."""


class PluginNotFoundError(PluginError):
    """Raised when a requested plugin is not found in an entry point group."""


class PluginLoadError(PluginError):
    """Raised when an entry point fails to import or load."""


def _get_entry_points(group: str) -> list[Any]:
    """Retrieve entry points for a group across Python version differences."""
    try:
        eps = importlib.metadata.entry_points(group=group)
        return list(eps)
    except TypeError:
        eps = importlib.metadata.entry_points()
        if hasattr(eps, "select"):
            return list(eps.select(group=group))
        if isinstance(eps, dict):
            return list(eps.get(group, []))
        return [ep for ep in eps if getattr(ep, "group", None) == group]


class PluginRegistry:
    """Discovers, caches, and resolves entry-point plugins for Agent OS."""

    def __init__(self) -> None:
        self._cache: dict[str, dict[str, Any]] = {}

    def discover(self, group: str) -> dict[str, Any]:
        """Discover and validate all entry points in a given group.

        Returns a dictionary mapping plugin name to its EntryPoint object.
        Fails closed if duplicate plugin names exist within the group.
        """
        if group in self._cache:
            return dict(self._cache[group])

        entry_points = _get_entry_points(group)
        discovered: dict[str, Any] = {}

        for ep in entry_points:
            name = ep.name.strip()
            if name in discovered:
                raise PluginDiscoveryError(
                    f"Duplicate plugin name '{name}' discovered in entry point group '{group}'."
                )
            discovered[name] = ep

        self._cache[group] = discovered
        return dict(discovered)

    def list_available(self, group: str) -> list[str]:
        """Return a sorted list of available plugin names in a group."""
        return sorted(self.discover(group).keys())

    def resolve(self, group: str, name: str) -> Any:
        """Resolve and load a plugin by group and name.

        Returns the loaded object (class, function, or instance).
        Raises PluginNotFoundError if the name is not in the group.
        Raises PluginLoadError if the entry point fails to import or load.
        """
        discovered = self.discover(group)
        clean_name = name.strip()

        if clean_name not in discovered:
            available = ", ".join(self.list_available(group)) or "none"
            raise PluginNotFoundError(
                f"Plugin '{clean_name}' not found in group '{group}'. "
                f"Available plugins: {available}."
            )

        ep = discovered[clean_name]
        try:
            return ep.load()
        except Exception as exc:
            raise PluginLoadError(
                f"Failed to load plugin '{clean_name}' from group '{group}': {exc}"
            ) from exc
