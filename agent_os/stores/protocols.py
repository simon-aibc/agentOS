"""Storage protocol definitions for Agent OS (Phase 0 storage seam)."""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from agent_os.principal import Principal

if TYPE_CHECKING:
    from agent_os.observations import Observation, StrategyAssignment
    from agent_os.permission_store import PermissionRule


@runtime_checkable
class RunStore(Protocol):
    """Protocol for managing run executions, events, and approvals."""

    def create_run(
        self,
        thread_id: str,
        workspace: str | None,
        task: str | None,
        *,
        task_signature: str | None = None,
        workspace_id: str | None = None,
        created_by: str | None = None,
        created_by_kind: str | None = None,
        principal: Principal | None = None,
    ) -> str: ...

    def append_event(self, run_id: str, kind: str, payload: dict[str, Any]) -> int: ...

    def set_status(
        self,
        run_id: str,
        status: str,
        *,
        error: str | None = None,
        ended: bool = False,
    ) -> None: ...

    def transition_status(
        self,
        run_id: str,
        *,
        expected: str,
        status: str,
        error: str | None = None,
        ended: bool = False,
    ) -> bool: ...

    def get_run(self, run_id: str) -> dict[str, Any] | None: ...

    def list_runs(
        self,
        *,
        status: str | None = None,
        workspace: str | None = None,
        workspace_id: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]: ...

    def list_events(self, run_id: str, *, after: int = 0) -> list[dict[str, Any]]: ...

    def record_approval(
        self,
        run_id: str,
        *,
        decision: str,
        actor_id: str,
        actor_kind: str,
        on_behalf_of: str | None = None,
        reason: str | None = None,
    ) -> int: ...

    def record_approval_and_transition(
        self,
        run_id: str,
        *,
        decision: str = "approved",
        actor_id: str,
        actor_kind: str,
        on_behalf_of: str | None = None,
        reason: str | None = None,
        expected: str = "interrupted",
        status: str = "running",
        error: str | None = None,
        ended: bool = False,
    ) -> bool: ...

    def record_cancellation_and_transition(
        self,
        run_id: str,
        *,
        actor_id: str,
        actor_kind: str,
        on_behalf_of: str | None = None,
        reason: str | None = None,
    ) -> bool: ...

    def list_approvals(self, run_id: str) -> list[dict[str, Any]]: ...


@runtime_checkable
class ScheduleStore(Protocol):
    """Protocol for managing persistent schedules."""

    def create_schedule(
        self,
        *,
        name: str,
        kind: str,
        trigger_kind: str,
        trigger_value: str,
        timezone: str = "UTC",
        payload: dict[str, Any] | None = None,
        enabled: bool = True,
    ) -> str: ...

    def get_schedule(self, schedule_id: str) -> dict[str, Any] | None: ...

    def list_schedules(
        self,
        *,
        kind: str | None = None,
        enabled: bool | None = None,
    ) -> list[dict[str, Any]]: ...

    def remove_schedule(self, schedule_id: str) -> bool: ...

    def set_schedule_enabled(self, schedule_id: str, enabled: bool) -> bool: ...

    def claim_due_schedules(
        self,
        now_utc: dt.datetime | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]: ...

    def record_schedule_result(
        self,
        schedule_id: str,
        *,
        status: str,
        error: str | None = None,
    ) -> None: ...


@runtime_checkable
class PermissionStore(Protocol):
    """Protocol for managing learned permission rules."""

    def get(self, permission_key: str) -> PermissionRule | None: ...

    def upsert(self, rule: PermissionRule) -> None: ...

    def list(self) -> list[PermissionRule]: ...

    def delete(self, permission_key: str) -> bool: ...

    def increment_approve(self, permission_key: str) -> None: ...

    def increment_deny(self, permission_key: str) -> None: ...


@runtime_checkable
class ObservationStore(Protocol):
    """Protocol for recording operational evidence and strategy assignments."""

    def create(
        self,
        *,
        workspace_id: str,
        run_id: str | None,
        thread_id: str | None,
        task_kind: str,
        task_signature: str | None = None,
        approach: str,
        artifact_refs: list[str] | None = None,
        outcome_signal: str = "unknown",
        outcome_evidence: str | None = None,
        source: str,
    ) -> Observation: ...

    def list(
        self,
        *,
        workspace_id: str,
        task_kind: str | None = None,
        limit: int = 100,
    ) -> list[Observation]: ...

    def get(self, observation_id: str) -> Observation | None: ...

    def record_outcome(
        self,
        *,
        observation_id: str,
        outcome_signal: str,
        outcome_evidence: str | None = None,
    ) -> Observation: ...

    def assign_strategy(
        self,
        *,
        workspace_id: str,
        task_kind: str,
        run_id: str,
        strategy_id: str,
        strategy_version: int = 1,
        selection_reason: str = "default",
        selector_version: str = "v1.0",
        evidence_summary: dict[str, Any] | None = None,
    ) -> str: ...

    def get_strategy_assignment(self, run_id: str) -> StrategyAssignment | None: ...
