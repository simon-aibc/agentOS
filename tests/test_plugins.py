from typing import Any

import pytest

from agent_os.plugins import (
    PluginDiscoveryError,
    PluginLoadError,
    PluginNotFoundError,
    PluginRegistry,
)


class DummyEntryPoint:
    def __init__(self, name: str, value: str, target: Any = None, error: Exception | None = None):
        self.name = name
        self.value = value
        self.target = target
        self.error = error

    def load(self) -> Any:
        if self.error:
            raise self.error
        return self.target


def test_plugin_registry_discovery_and_caching(monkeypatch):
    registry = PluginRegistry()

    eps = [
        DummyEntryPoint("redis", "my_pkg.redis:RedisConnector", target="redis_cls"),
        DummyEntryPoint("pg", "my_pkg.pg:PgConnector", target="pg_cls"),
    ]

    call_count = 0

    def mock_get_entry_points(group: str):
        nonlocal call_count
        call_count += 1
        if group == "agent_os.connectors":
            return eps
        return []

    monkeypatch.setattr("agent_os.plugins._get_entry_points", mock_get_entry_points)

    # First discovery
    discovered = registry.discover("agent_os.connectors")
    assert len(discovered) == 2
    assert "redis" in discovered
    assert "pg" in discovered
    assert call_count == 1

    # Second discovery should hit cache without calling entry_points again
    cached = registry.discover("agent_os.connectors")
    assert cached == discovered
    assert call_count == 1

    assert registry.list_available("agent_os.connectors") == ["pg", "redis"]


def test_plugin_registry_duplicate_names_fail_closed(monkeypatch):
    registry = PluginRegistry()

    duplicate_eps = [
        DummyEntryPoint("custom", "pkg_a:Custom"),
        DummyEntryPoint("custom", "pkg_b:Custom"),
    ]

    monkeypatch.setattr(
        "agent_os.plugins._get_entry_points",
        lambda group: duplicate_eps if group == "agent_os.connectors" else [],
    )

    with pytest.raises(PluginDiscoveryError, match="Duplicate plugin name 'custom' discovered in entry point group 'agent_os.connectors'"):
        registry.discover("agent_os.connectors")


def test_plugin_registry_resolve_success(monkeypatch):
    registry = PluginRegistry()

    class CustomMemory:
        name = "custom_mem"

    eps = [
        DummyEntryPoint("custom_mem", "pkg:CustomMemory", target=CustomMemory),
    ]

    monkeypatch.setattr(
        "agent_os.plugins._get_entry_points",
        lambda group: eps if group == "agent_os.memory_connectors" else [],
    )

    resolved = registry.resolve("agent_os.memory_connectors", "custom_mem")
    assert resolved is CustomMemory


def test_plugin_registry_resolve_not_found_actionable_error(monkeypatch):
    registry = PluginRegistry()

    eps = [
        DummyEntryPoint("redis", "pkg:Redis"),
    ]

    monkeypatch.setattr(
        "agent_os.plugins._get_entry_points",
        lambda group: eps if group == "agent_os.connectors" else [],
    )

    with pytest.raises(PluginNotFoundError) as exc_info:
        registry.resolve("agent_os.connectors", "missing_connector")

    msg = str(exc_info.value)
    assert "Plugin 'missing_connector' not found in group 'agent_os.connectors'" in msg
    assert "Available plugins: redis" in msg


def test_plugin_registry_resolve_load_failure_actionable_error(monkeypatch):
    registry = PluginRegistry()

    eps = [
        DummyEntryPoint(
            "broken",
            "pkg.broken:Broken",
            error=ImportError("No module named 'some_missing_dependency'"),
        ),
    ]

    monkeypatch.setattr(
        "agent_os.plugins._get_entry_points",
        lambda group: eps if group == "agent_os.connectors" else [],
    )

    with pytest.raises(PluginLoadError) as exc_info:
        registry.resolve("agent_os.connectors", "broken")

    msg = str(exc_info.value)
    assert "Failed to load plugin 'broken' from group 'agent_os.connectors'" in msg
    assert "No module named 'some_missing_dependency'" in msg
