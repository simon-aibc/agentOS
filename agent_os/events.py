"""Lifecycle event definitions and egress dispatcher for Agent OS (Phase 4)."""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger(__name__)

SUPPORTED_LIFECYCLE_EVENTS = frozenset(
    {
        "run.created",
        "run.interrupted",
        "run.approved",
        "run.completed",
        "run.failed",
        "run.cancelled",
    }
)


@runtime_checkable
class EventSink(Protocol):
    """Protocol for egressing lifecycle events to external systems."""

    @property
    def name(self) -> str: ...

    def emit(self, event: dict[str, Any]) -> None: ...


def build_lifecycle_payload(
    event_type: str,
    *,
    run_id: str,
    workspace_id: str,
    status: str,
    actor_id: str = "local:operator",
    actor_kind: str = "human",
    actor_display: str = "operator",
    on_behalf_of: str | None = None,
    thread_id: str | None = None,
    timestamp: str | None = None,
) -> dict[str, Any]:
    """Build a sanitized lifecycle event payload enforcing privacy invariants.

    NEVER includes task text, model output, tool arguments/results, approval reasons,
    tokens, context contents, or arbitrary payloads.
    """
    if event_type not in SUPPORTED_LIFECYCLE_EVENTS:
        raise ValueError(
            f"Unsupported lifecycle event type '{event_type}'. "
            f"Supported: {sorted(SUPPORTED_LIFECYCLE_EVENTS)}"
        )

    now = timestamp or dt.datetime.now(dt.UTC).isoformat()
    actor_data: dict[str, Any] = {
        "id": str(actor_id),
        "kind": str(actor_kind),
        "display": str(actor_display),
    }
    if on_behalf_of:
        actor_data["on_behalf_of"] = str(on_behalf_of)

    references: dict[str, Any] = {}
    if thread_id:
        references["thread_id"] = str(thread_id)

    return {
        "event": event_type,
        "run_id": str(run_id),
        "workspace_id": str(workspace_id),
        "status": str(status),
        "timestamp": now,
        "actor": actor_data,
        "references": references,
    }


def dispatch_event(
    sinks: list[EventSink] | tuple[EventSink, ...],
    event: dict[str, Any],
) -> None:
    """Dispatch a sanitized lifecycle event to all configured sinks with fail-soft guarantees."""
    if not sinks:
        return

    for sink in sinks:
        sink_name = getattr(sink, "name", "unknown")
        try:
            sink.emit(event)
        except Exception as exc:
            logger.warning(
                f"Event sink '{sink_name}' failed to emit event '{event.get('event')}': {exc}"
            )


def emit_run_lifecycle_event(
    event_type: str,
    *,
    run_id: str,
    workspace_id: str,
    status: str,
    principal: Any = None,
    run: dict[str, Any] | None = None,
    thread_id: str | None = None,
    sinks: list[EventSink] | tuple[EventSink, ...] | None = None,
) -> None:
    """Construct and dispatch a sanitized lifecycle event to active workspace event sinks."""
    actor_id = "local:operator"
    actor_kind = "human"
    actor_display = "operator"
    on_behalf_of = None

    if principal is not None:
        actor_id = getattr(principal, "id", str(principal))
        actor_kind = getattr(principal, "kind", "human")
        actor_display = getattr(principal, "display", str(actor_id))
        on_behalf_of = getattr(principal, "on_behalf_of", None)
    elif run is not None:
        if run.get("created_by"):
            actor_id = str(run["created_by"])
        if run.get("created_by_kind"):
            actor_kind = str(run["created_by_kind"])
        actor_display = actor_id
        if not thread_id:
            thread_id = str(run.get("thread_id") or "")

    payload = build_lifecycle_payload(
        event_type,
        run_id=run_id,
        workspace_id=workspace_id,
        status=status,
        actor_id=actor_id,
        actor_kind=actor_kind,
        actor_display=actor_display,
        on_behalf_of=on_behalf_of,
        thread_id=thread_id,
    )

    if sinks is None:
        try:
            from agent_os.server.runtime import composed_workspace

            runtime = composed_workspace()
            if runtime is not None:
                sinks = getattr(runtime, "event_sinks", ())
        except Exception as exc:
            logger.debug(f"Could not retrieve runtime event sinks: {exc}")
            sinks = ()

    if sinks:
        dispatch_event(sinks, payload)

