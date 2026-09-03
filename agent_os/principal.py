"""Principal identity model and resolvers for Agent OS (Phase 0).

Records auditable identity for local operators, scheduled jobs, and HTTP requests
without storing, exposing, or deriving identifiers from raw secret tokens.
"""

from __future__ import annotations

import getpass
import ipaddress
import os
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class Principal:
    """An auditable actor or security identity initiating or approving work."""

    id: str
    kind: str  # "human" | "service" | "schedule" | "system"
    display: str
    on_behalf_of: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id.strip():
            raise ValueError("Principal.id must be a non-empty string")
        if not isinstance(self.kind, str) or not self.kind.strip():
            raise ValueError("Principal.kind must be a non-empty string")
        if not isinstance(self.display, str) or not self.display.strip():
            raise ValueError("Principal.display must be a non-empty string")


@runtime_checkable
class PrincipalResolver(Protocol):
    """Protocol for resolving the initiating or acting principal from context."""

    def resolve(self, request_or_context: Any = None) -> Principal:
        ...


def _get_local_os_user() -> str:
    try:
        user = getpass.getuser().strip()
        if user:
            return user
    except Exception:
        pass
    return os.getenv("USER", "").strip() or os.getenv("USERNAME", "").strip() or "operator"


def _is_loopback_client(host: object) -> bool:
    if not isinstance(host, str):
        return False
    normalized = host.strip().lower()
    if normalized in {"localhost", "testclient"}:
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


class LocalPrincipalResolver:
    """Default reference resolver for local CLI, scheduler, and HTTP environments."""

    def resolve(self, request_or_context: Any = None) -> Principal:
        # If a Principal is already passed, return it
        if isinstance(request_or_context, Principal):
            return request_or_context

        # Scheduler context: dict or object with schedule_id
        if isinstance(request_or_context, dict) and "schedule_id" in request_or_context:
            sched_id = str(request_or_context["schedule_id"]).strip()
            return Principal(
                id=f"schedule:{sched_id}",
                kind="schedule",
                display=f"Schedule ({sched_id})",
            )

        if hasattr(request_or_context, "schedule_id") and request_or_context.schedule_id:
            sched_id = str(request_or_context.schedule_id).strip()
            return Principal(
                id=f"schedule:{sched_id}",
                kind="schedule",
                display=f"Schedule ({sched_id})",
            )

        # Explicit schedule string: "schedule:<id>"
        if isinstance(request_or_context, str) and request_or_context.startswith("schedule:"):
            sched_id = request_or_context.removeprefix("schedule:").strip()
            return Principal(
                id=f"schedule:{sched_id}",
                kind="schedule",
                display=f"Schedule ({sched_id})",
            )

        # HTTP request context (FastAPI/Starlette Request or context with trusted state/client)
        # Note: LocalPrincipalResolver NEVER reads raw credentials or client headers (x-execution-token, Authorization, etc.)
        if request_or_context is not None and (
            hasattr(request_or_context, "state") or hasattr(request_or_context, "client")
        ):
            state = getattr(request_or_context, "state", None)
            authenticated_with_token = False
            is_loopback = None
            if state is not None:
                authenticated_with_token = bool(getattr(state, "authenticated_with_token", False))
                is_loopback = getattr(state, "is_loopback", None)

            if is_loopback is None:
                client = getattr(request_or_context, "client", None)
                client_host = client.host if client is not None else None
                is_loopback = _is_loopback_client(client_host) if client_host else False

            if not authenticated_with_token and is_loopback:
                user = _get_local_os_user()
                return Principal(
                    id=f"local:{user}",
                    kind="human",
                    display=f"Local User ({user})",
                    on_behalf_of=None,
                )

            return Principal(
                id="token:execution",
                kind="service",
                display="Execution Token",
                on_behalf_of=None,
            )

        # Default / CLI context
        user = _get_local_os_user()
        return Principal(
            id=f"local:{user}",
            kind="human",
            display=f"Local User ({user})",
        )
