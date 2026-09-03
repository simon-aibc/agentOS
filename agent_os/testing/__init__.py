"""Public test and conformance utilities for Agent OS extensions."""

from agent_os.testing.conformance import (
    BackendAdapterConformanceMixin,
    ConformanceError,
    ConnectorConformanceMixin,
    ContextProviderConformanceMixin,
    EventSinkConformanceMixin,
    IndexableMemoryConformanceMixin,
    MemoryConnectorConformanceMixin,
    check_backend_adapter,
    check_connector,
    check_context_provider,
    check_event_sink,
    check_indexable_memory,
    check_memory_connector,
)

__all__ = (
    "BackendAdapterConformanceMixin",
    "ConformanceError",
    "ConnectorConformanceMixin",
    "ContextProviderConformanceMixin",
    "EventSinkConformanceMixin",
    "IndexableMemoryConformanceMixin",
    "MemoryConnectorConformanceMixin",
    "check_backend_adapter",
    "check_connector",
    "check_context_provider",
    "check_event_sink",
    "check_indexable_memory",
    "check_memory_connector",
)
