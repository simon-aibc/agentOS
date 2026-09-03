"""SQLite reference implementation of RunStore for Agent OS."""

from __future__ import annotations

from typing import Any

from agent_os import runs
from agent_os.principal import Principal


class SqliteRunStore:
    """SQLite run ledger store conforming to RunStore protocol."""

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
    ) -> str:
        return runs.create_run(
            thread_id,
            workspace,
            task,
            task_signature=task_signature,
            workspace_id=workspace_id,
            created_by=created_by,
            created_by_kind=created_by_kind,
            principal=principal,
        )

    def append_event(self, run_id: str, kind: str, payload: dict[str, Any]) -> int:
        return runs.append_event(run_id, kind, payload)

    def set_status(
        self,
        run_id: str,
        status: str,
        *,
        error: str | None = None,
        ended: bool = False,
    ) -> None:
        runs.set_status(run_id, status, error=error, ended=ended)

    def transition_status(
        self,
        run_id: str,
        *,
        expected: str,
        status: str,
        error: str | None = None,
        ended: bool = False,
    ) -> bool:
        return runs.transition_status(
            run_id,
            expected=expected,
            status=status,
            error=error,
            ended=ended,
        )

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        return runs.get_run(run_id)

    def list_runs(
        self,
        *,
        status: str | None = None,
        workspace: str | None = None,
        workspace_id: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        return runs.list_runs(
            status=status,
            workspace=workspace,
            workspace_id=workspace_id,
            limit=limit,
        )

    def list_events(self, run_id: str, *, after: int = 0) -> list[dict[str, Any]]:
        return runs.list_events(run_id, after=after)

    def record_approval(
        self,
        run_id: str,
        *,
        decision: str,
        actor_id: str,
        actor_kind: str,
        on_behalf_of: str | None = None,
        reason: str | None = None,
    ) -> int:
        return runs.record_approval(
            run_id,
            decision=decision,
            actor_id=actor_id,
            actor_kind=actor_kind,
            on_behalf_of=on_behalf_of,
            reason=reason,
        )

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
    ) -> bool:
        return runs.record_approval_and_transition(
            run_id,
            decision=decision,
            actor_id=actor_id,
            actor_kind=actor_kind,
            on_behalf_of=on_behalf_of,
            reason=reason,
            expected=expected,
            status=status,
            error=error,
            ended=ended,
        )

    def record_cancellation_and_transition(
        self,
        run_id: str,
        *,
        actor_id: str,
        actor_kind: str,
        on_behalf_of: str | None = None,
        reason: str | None = None,
    ) -> bool:
        return runs.record_cancellation_and_transition(
            run_id,
            actor_id=actor_id,
            actor_kind=actor_kind,
            on_behalf_of=on_behalf_of,
            reason=reason,
        )

    def list_approvals(self, run_id: str) -> list[dict[str, Any]]:
        return runs.list_approvals(run_id)
