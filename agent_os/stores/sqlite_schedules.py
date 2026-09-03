"""SQLite reference implementation of ScheduleStore for Agent OS."""

from __future__ import annotations

import datetime as dt
from typing import Any

from agent_os import schedules


class SqliteScheduleStore:
    """SQLite schedule store conforming to ScheduleStore protocol."""

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
    ) -> str:
        return schedules.create_schedule(
            name=name,
            kind=kind,
            trigger_kind=trigger_kind,
            trigger_value=trigger_value,
            timezone=timezone,
            payload=payload,
            enabled=enabled,
        )

    def get_schedule(self, schedule_id: str) -> dict[str, Any] | None:
        return schedules.get_schedule(schedule_id)

    def list_schedules(
        self,
        *,
        kind: str | None = None,
        enabled: bool | None = None,
    ) -> list[dict[str, Any]]:
        return schedules.list_schedules(
            kind=kind,
            enabled=enabled,
        )

    def remove_schedule(self, schedule_id: str) -> bool:
        return schedules.remove_schedule(schedule_id)

    def set_schedule_enabled(self, schedule_id: str, enabled: bool) -> bool:
        return schedules.set_schedule_enabled(schedule_id, enabled)

    def claim_due_schedules(
        self,
        now_utc: dt.datetime | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        return schedules.claim_due_schedules(now_utc=now_utc, limit=limit)

    def record_schedule_result(
        self,
        schedule_id: str,
        *,
        status: str,
        error: str | None = None,
    ) -> None:
        schedules.record_schedule_result(
            schedule_id,
            status=status,
            error=error,
        )
