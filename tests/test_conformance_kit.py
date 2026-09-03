from pathlib import Path

import pytest

from agent_os.backends import AuthStatus
from agent_os.connectors import MarkdownVaultConnector
from agent_os.context import ContextBlock
from agent_os.events import dispatch_event
from agent_os.principal import Principal
from agent_os.testing import (
    ConformanceError,
    check_backend_adapter,
    check_connector,
    check_context_provider,
    check_event_sink,
    check_indexable_memory,
    check_memory_connector,
)
from agent_os.webhooks import WebhookEventSink


class ValidActionConnector:
    name = "valid_action"

    def capabilities(self):
        return {"actions": ["action_read", "action_write"]}

    def describe_side_effect(self, action: str) -> str:
        if action == "action_read":
            return "read"
        return "write"

    def invoke(self, action: str, params: dict):
        return {"status": "ok"}


class ValidContextProvider:
    name = "valid_provider"

    def provide(
        self,
        task: str,
        *,
        budget_chars: int,
        principal: Principal,
        workspace_id: str,
    ):
        return [
            ContextBlock(source="valid_provider", content=f"Context for {workspace_id}"),
            {"source": "dict_source", "content": "Dict content"},
            "Raw string content",
        ]


class ValidBackendAdapter:
    name = "valid_backend"
    binary_name = "valid-bin"
    supported_roles = frozenset({"architect", "executor"})
    stub = False

    def authentication_status(self) -> AuthStatus:
        return AuthStatus(status="ok", detail="configured via env")

    def build_invoker(self, role: str):
        return lambda state: {"status": "completed"}


class ValidEventSink:
    name = "valid_sink"

    def emit(self, event: dict):
        pass


class ValidMemoryConnector:
    name = "valid_memory"

    def list_notes(self):
        return [{"ref": "note1.md", "title": "Note 1"}]

    def read_note(self, ref: str):
        return {"ref": ref, "content": "Sample content"}

    def search(self, query: str, limit: int = 5):
        if limit <= 0:
            return []
        return [{"ref": "note1.md", "content": "Sample", "score": 1.0}]


def test_conformance_accepts_valid_builtins_and_references(tmp_path: Path):
    # 1. Memory connectors
    vault = MarkdownVaultConnector(str(tmp_path))
    vault.write_note("note.md", "Sample content")
    check_memory_connector(vault)
    check_indexable_memory(vault)

    check_memory_connector(ValidMemoryConnector())

    # 2. Action connectors
    check_connector(ValidActionConnector())

    # 3. Context provider
    check_context_provider(ValidContextProvider())

    # 4. Backend adapter
    check_backend_adapter(ValidBackendAdapter())

    # 5. Event sink
    check_event_sink(ValidEventSink())
    webhook_sink = WebhookEventSink(
        url="https://example.com/webhook",
        secret="key",
    )
    check_event_sink(webhook_sink)


def test_conformance_rejects_malformed_memory_connector():
    class MissingMethodsMemory:
        name = "bad_memory"

    with pytest.raises(ConformanceError, match="missing callable method 'search'"):
        check_memory_connector(MissingMethodsMemory())

    class BadReturnMemory:
        name = "bad_memory"

        def search(self, query, limit=5):
            return "not-a-list"

        def read_note(self, ref):
            return {}

        def list_notes(self):
            return []

    with pytest.raises(ConformanceError, match="search.*must return a list"):
        check_memory_connector(BadReturnMemory())


def test_conformance_rejects_invalid_side_effect_taxonomy():
    class InvalidTaxonomyConnector:
        name = "bad_action"

        def capabilities(self):
            return {"actions": ["destroy"]}

        def describe_side_effect(self, action: str) -> str:
            return "destructive_tier_9"  # Not in the 7-value taxonomy!

        def invoke(self, action: str, params: dict):
            return {}

    with pytest.raises(
        ConformanceError,
        match="declared invalid side effect 'destructive_tier_9'.*Must be exactly one of the 7 supported side effects",
    ):
        check_connector(InvalidTaxonomyConnector())


def test_conformance_rejects_malformed_context_provider():
    class BadContextProvider:
        name = "bad_ctx"

        def provide(self, task, *, budget_chars, principal, workspace_id):
            return 12345

    with pytest.raises(ConformanceError, match="returned invalid type int"):
        check_context_provider(BadContextProvider())

    class BadListElementContextProvider:
        name = "bad_list_ctx"

        def provide(self, task, *, budget_chars, principal, workspace_id):
            return [12345]  # Invalid element

    with pytest.raises(ConformanceError, match="list element at index 0 has invalid type int"):
        check_context_provider(BadListElementContextProvider())


def test_conformance_rejects_malformed_backend_adapter():
    class BadBackendAdapter:
        name = "bad_backend"
        binary_name = "bin"
        supported_roles = frozenset({"unknown_role"})

        def authentication_status(self):
            return AuthStatus(status="ok")

        def build_invoker(self, role):
            return lambda state: None

    with pytest.raises(ConformanceError, match="is not a valid BackendRole"):
        check_backend_adapter(BadBackendAdapter())

    class NonCallableInvokerAdapter:
        name = "bad_backend_2"
        binary_name = "bin"
        supported_roles = frozenset({"executor"})

        def authentication_status(self):
            return AuthStatus(status="ok")

        def build_invoker(self, role):
            return "not-a-callable"

    with pytest.raises(ConformanceError, match="must return a callable"):
        check_backend_adapter(NonCallableInvokerAdapter())


def test_conformance_rejects_malformed_event_sink():
    class BadEventSink:
        name = "bad_sink"
        # Missing emit()

    with pytest.raises(ConformanceError, match="missing callable method 'emit'"):
        check_event_sink(BadEventSink())


def test_dispatch_event_with_failing_sink_is_fail_soft():
    class ThrowingSink:
        name = "throwing_sink"

        def emit(self, event: dict):
            raise RuntimeError("Sink exploded unexpectedly!")

    sample_event = {
        "event": "run.failed",
        "run_id": "test-run-failsoft",
        "workspace_id": "ws-test",
        "status": "failed",
    }
    # Dispatcher catches sink exceptions and does not crash caller
    dispatch_event([ThrowingSink()], sample_event)
