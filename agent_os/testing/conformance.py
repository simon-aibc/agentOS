"""Public, dependency-light extension conformance test kit for Agent OS (Phase 5).

Satellite packages and third-party extension authors can import these helpers
to verify their implementations satisfy Agent OS public extension contracts
in their own standalone CI without requiring workspace internals or real networks.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from agent_os.api import (
    SUPPORTED_SIDE_EFFECTS,
    AuthStatus,
    ContextBlock,
    IndexResult,
    Principal,
)


class ConformanceError(AssertionError):
    """Raised when an extension fails to satisfy the Agent OS public contract."""


def check_memory_connector(connector: Any) -> None:
    """Verify an object satisfies the public MemoryConnector contract."""
    if not hasattr(connector, "name") or not isinstance(connector.name, str) or not connector.name.strip():
        raise ConformanceError("MemoryConnector must have a non-empty string 'name' attribute.")

    for method in ("search", "read_note", "list_notes"):
        if not hasattr(connector, method) or not callable(getattr(connector, method)):
            raise ConformanceError(f"MemoryConnector '{connector.name}' missing callable method '{method}'.")

    # 1. list_notes contract
    try:
        notes = connector.list_notes()
    except Exception as exc:
        raise ConformanceError(f"MemoryConnector.list_notes() raised an exception: {exc}") from exc

    if not isinstance(notes, (list, tuple)):
        raise ConformanceError(f"MemoryConnector.list_notes() must return a list or tuple, got {type(notes).__name__}.")

    for note in notes:
        if not isinstance(note, dict) or "ref" not in note:
            raise ConformanceError(f"Notes returned by list_notes() must be dicts with a 'ref' key, got: {note}")

    # 2. search contract
    try:
        hits = connector.search("test_conformance_query", limit=5)
    except Exception as exc:
        raise ConformanceError(f"MemoryConnector.search() raised an exception: {exc}") from exc

    if not isinstance(hits, (list, tuple)):
        raise ConformanceError(f"MemoryConnector.search() must return a list, got {type(hits).__name__}.")

    for hit in hits:
        if not isinstance(hit, dict) or "ref" not in hit:
            raise ConformanceError(f"Hits returned by search() must be dicts with a 'ref' key, got: {hit}")

    # 3. bounded search contract (limit <= 0 must not crash)
    try:
        empty_hits = connector.search("test", limit=0)
        if not isinstance(empty_hits, (list, tuple)):
            raise ConformanceError("MemoryConnector.search() with limit=0 must return a list.")
    except Exception as exc:
        raise ConformanceError(f"MemoryConnector.search(..., limit=0) raised an exception: {exc}") from exc


def check_connector(connector: Any) -> None:
    """Verify an object satisfies the public Action Connector contract."""
    if not hasattr(connector, "name") or not isinstance(connector.name, str) or not connector.name.strip():
        raise ConformanceError("Connector must have a non-empty string 'name' attribute.")

    for method in ("capabilities", "describe_side_effect", "invoke"):
        if not hasattr(connector, method) or not callable(getattr(connector, method)):
            raise ConformanceError(f"Connector '{connector.name}' missing callable method '{method}'.")

    try:
        caps = connector.capabilities()
    except Exception as exc:
        raise ConformanceError(f"Connector.capabilities() raised an exception: {exc}") from exc

    if not isinstance(caps, Mapping):
        raise ConformanceError(
            "Connector.capabilities() must return a mapping with an 'actions' sequence, "
            f"got {type(caps).__name__}."
        )

    actions = caps.get("actions")
    if not isinstance(actions, (list, tuple, set, frozenset)):
        raise ConformanceError(
            "Connector.capabilities()['actions'] must be a sequence or set."
        )

    for action in actions:
        try:
            side_effect = connector.describe_side_effect(action)
        except Exception as exc:
            raise ConformanceError(f"describe_side_effect('{action}') raised an exception: {exc}") from exc

        if not isinstance(side_effect, str) or side_effect not in SUPPORTED_SIDE_EFFECTS:
            raise ConformanceError(
                f"Connector action '{action}' declared invalid side effect '{side_effect}'. "
                f"Must be exactly one of the 7 supported side effects: {sorted(SUPPORTED_SIDE_EFFECTS)}."
            )


def check_context_provider(provider: Any) -> None:
    """Verify an object satisfies the public ContextProvider contract."""
    if not hasattr(provider, "name") or not isinstance(provider.name, str) or not provider.name.strip():
        raise ConformanceError("ContextProvider must have a non-empty string 'name' attribute.")

    if not hasattr(provider, "provide") or not callable(provider.provide):
        raise ConformanceError(f"ContextProvider '{provider.name}' missing callable method 'provide'.")

    principal = Principal(id="user:conformance", kind="human", display="Conformance Tester")
    try:
        res = provider.provide(
            "Sample task description for context conformance",
            budget_chars=1000,
            principal=principal,
            workspace_id="test-conformance-ws",
        )
    except Exception as exc:
        raise ConformanceError(f"ContextProvider.provide() raised an exception: {exc}") from exc

    valid_top_types = (str, ContextBlock, list, tuple)
    if not isinstance(res, valid_top_types):
        raise ConformanceError(
            f"ContextProvider.provide() returned invalid type {type(res).__name__}. "
            "Must be ContextBlock, list/tuple of supported blocks, or str (raw dict is not allowed)."
        )

    if isinstance(res, (list, tuple)):
        for idx, item in enumerate(res):
            if isinstance(item, (ContextBlock, str)):
                continue
            if isinstance(item, dict):
                content = item.get("content") or item.get("text")
                if not content or not isinstance(content, str) or not content.strip():
                    raise ConformanceError(
                        f"ContextProvider.provide() list element at index {idx} dict must have a non-empty string 'content' or 'text' key."
                    )
            else:
                raise ConformanceError(
                    f"ContextProvider.provide() list element at index {idx} has invalid type {type(item).__name__}. "
                    "Allowed element types are ContextBlock, str, or dict."
                )


def check_indexable_memory(memory: Any) -> None:
    """Verify an object satisfies the public IndexableMemory contract."""
    for method in ("index", "reindex", "index_status"):
        if not hasattr(memory, method) or not callable(getattr(memory, method)):
            raise ConformanceError(f"IndexableMemory missing callable method '{method}'.")

    try:
        status = memory.index_status()
    except Exception as exc:
        raise ConformanceError(f"IndexableMemory.index_status() raised an exception: {exc}") from exc

    if not isinstance(status, dict) or "status" not in status or "document_count" not in status:
        raise ConformanceError(
            "IndexableMemory.index_status() must return a dict with 'status' (str) and 'document_count' (int) keys."
        )

    try:
        res = memory.index()
    except Exception as exc:
        raise ConformanceError(f"IndexableMemory.index() raised an exception: {exc}") from exc

    if not isinstance(res, IndexResult):
        raise ConformanceError(
            f"IndexableMemory.index() must return an IndexResult instance, got {type(res).__name__}."
        )

    try:
        reindex_result = memory.reindex()
    except Exception as exc:
        raise ConformanceError(f"IndexableMemory.reindex() raised an exception: {exc}") from exc

    if not isinstance(reindex_result, IndexResult):
        raise ConformanceError(
            f"IndexableMemory.reindex() must return an IndexResult instance, got {type(reindex_result).__name__}."
        )


def check_backend_adapter(adapter: Any) -> None:
    """Verify an object satisfies the public BackendAdapter contract."""
    if not hasattr(adapter, "name") or not isinstance(adapter.name, str) or not adapter.name.strip():
        raise ConformanceError("BackendAdapter must have a non-empty string 'name' attribute.")

    if not hasattr(adapter, "binary_name") or not isinstance(adapter.binary_name, str):
        raise ConformanceError(f"BackendAdapter '{adapter.name}' missing 'binary_name' string attribute.")

    if not hasattr(adapter, "supported_roles") or not isinstance(adapter.supported_roles, (set, frozenset)):
        raise ConformanceError(f"BackendAdapter '{adapter.name}' missing 'supported_roles' set/frozenset.")

    for role in adapter.supported_roles:
        if role not in ("architect", "executor"):
            raise ConformanceError(
                f"BackendAdapter '{adapter.name}' role '{role}' is not a valid BackendRole ('architect' or 'executor')."
            )

    for method in ("authentication_status", "build_invoker"):
        if not hasattr(adapter, method) or not callable(getattr(adapter, method)):
            raise ConformanceError(f"BackendAdapter '{adapter.name}' missing callable method '{method}'.")

    try:
        auth = adapter.authentication_status()
    except Exception as exc:
        raise ConformanceError(f"BackendAdapter.authentication_status() raised an exception: {exc}") from exc

    if not isinstance(auth, AuthStatus):
        raise ConformanceError(
            f"BackendAdapter.authentication_status() must return an AuthStatus instance, got {type(auth).__name__}."
        )

    for role in adapter.supported_roles:
        try:
            invoker = adapter.build_invoker(role)
        except Exception as exc:
            raise ConformanceError(
                f"BackendAdapter.build_invoker('{role}') raised an exception: {exc}"
            ) from exc
        if not callable(invoker):
            raise ConformanceError(
                f"BackendAdapter.build_invoker('{role}') must return a callable."
            )


def check_event_sink(sink: Any) -> None:
    """Verify an object satisfies the public EventSink contract."""
    if not hasattr(sink, "name") or not isinstance(sink.name, str) or not sink.name.strip():
        raise ConformanceError("EventSink must have a non-empty string 'name' attribute.")

    if not hasattr(sink, "emit") or not callable(sink.emit):
        raise ConformanceError(f"EventSink '{sink.name}' missing callable method 'emit'.")

    sample_event = {
        "event": "run.created",
        "run_id": "conformance-run-1",
        "workspace_id": "conformance-ws-1",
        "status": "queued",
        "timestamp": "2026-01-01T00:00:00Z",
        "actor": {"id": "user:conformance", "kind": "human", "display": "Tester"},
        "references": {"thread_id": "conformance-thread-1"},
    }
    # Note: EventSink.emit may perform I/O and raise transport errors.
    # The runtime dispatcher handles fail-soft isolation.
    try:
        sink.emit(sample_event)
    except Exception:
        pass


# Pytest Mixins
class MemoryConnectorConformanceMixin:
    def test_memory_connector_conformance(self, connector: Any) -> None:
        check_memory_connector(connector)


class ConnectorConformanceMixin:
    def test_connector_conformance(self, connector: Any) -> None:
        check_connector(connector)


class ContextProviderConformanceMixin:
    def test_context_provider_conformance(self, provider: Any) -> None:
        check_context_provider(provider)


class IndexableMemoryConformanceMixin:
    def test_indexable_memory_conformance(self, memory: Any) -> None:
        check_indexable_memory(memory)


class BackendAdapterConformanceMixin:
    def test_backend_adapter_conformance(self, adapter: Any) -> None:
        check_backend_adapter(adapter)


class EventSinkConformanceMixin:
    def test_event_sink_conformance(self, sink: Any) -> None:
        check_event_sink(sink)
