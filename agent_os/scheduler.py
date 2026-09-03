"""Scheduler dispatcher, tick loop, and long-running service (r1.8a.3).

Turns claimed schedule rows into real work: ``run`` uses the v1.7 run
ledger and ``execute_run``; ``brief`` uses the r1.8a.2 shared seam.
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

from agent_os.brief_runtime import execute_brief
from agent_os.schedules import claim_due_schedules, record_schedule_result
from agent_os.server.run_executor import execute_run
from agent_os.stores import RunStore, SqliteRunStore

logger = logging.getLogger(__name__)


@dataclass
class ScheduleDispatchResult:
    schedule_id: str
    kind: str
    status: str
    run_id: str | None = None
    ref: str | None = None
    error: str | None = None


async def dispatch_schedule(
    schedule: dict[str, Any],
    *,
    manual: bool = False,
    run_store: RunStore | None = None,
) -> ScheduleDispatchResult:
    """Dispatch a single claimed schedule row."""
    schedule_id = schedule["schedule_id"]
    kind = schedule.get("kind", "run")
    payload = schedule.get("payload", {})
    store = run_store or SqliteRunStore()

    try:
        if kind == "run":
            thread_id = str(uuid.uuid4())
            workspace = payload.get("workspace")
            task = payload.get("task", "")
            from agent_os.principal import Principal

            run_id = store.create_run(
                thread_id,
                workspace,
                task,
                workspace_id=workspace,
                principal=Principal(
                    id=f"schedule:{schedule_id}",
                    kind="schedule",
                    display=f"Schedule ({schedule_id})",
                ),
            )
            try:
                await execute_run(run_id, thread_id, task)
            except asyncio.CancelledError:
                error = "Scheduler shutdown"
                store.append_event(run_id, "error", {"message": error})
                store.set_status(run_id, "error", error=error, ended=True)
                raise

            run_obj = store.get_run(run_id)
            status = (run_obj or {}).get("status", "error")
            error = (run_obj or {}).get("error")

            record_schedule_result(schedule_id, status=status, error=error)

            return ScheduleDispatchResult(
                schedule_id=schedule_id,
                kind=kind,
                status=status,
                run_id=run_id,
                error=error,
            )

        if kind == "brief":
            result = await asyncio.to_thread(execute_brief, write=True)
            if not result.saved:
                error_msg = result.error or "Brief write was not committed"
                record_schedule_result(schedule_id, status="error", error=error_msg)
                return ScheduleDispatchResult(
                    schedule_id=schedule_id,
                    kind=kind,
                    status="error",
                    error=error_msg,
                )

            record_schedule_result(schedule_id, status="completed")

            return ScheduleDispatchResult(
                schedule_id=schedule_id,
                kind=kind,
                status="completed",
                ref=result.ref,
            )

        if kind == "app_callback":
            url = payload.get("url", "")
            method = payload.get("method", "POST").upper()
            headers = payload.get("headers")
            body = payload.get("body")

            if not url:
                error_msg = "kind=app_callback requires 'url' in payload"
                record_schedule_result(schedule_id, status="error", error=error_msg)
                return ScheduleDispatchResult(
                    schedule_id=schedule_id,
                    kind=kind,
                    status="error",
                    error=error_msg,
                )

            try:
                async with httpx.AsyncClient(
                    timeout=30.0, follow_redirects=True, max_redirects=5
                ) as client:
                    response = await client.request(
                        method=method,
                        url=url,
                        headers=headers,
                        json=body if body is not None else None,
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "Callback request failed for schedule %s (%s)",
                    schedule_id,
                    type(exc).__name__,
                )
                error_msg = f"HTTP request failed: {type(exc).__name__}"
                record_schedule_result(schedule_id, status="error", error=error_msg)
                return ScheduleDispatchResult(
                    schedule_id=schedule_id,
                    kind=kind,
                    status="error",
                    error=error_msg,
                )

            if response.is_success:
                status = "completed"
                ref = str(response.status_code)
                record_schedule_result(schedule_id, status=status)
                return ScheduleDispatchResult(
                    schedule_id=schedule_id,
                    kind=kind,
                    status=status,
                    ref=ref,
                )
            else:
                status = "error"
                error_msg = f"HTTP {response.status_code}"
                ref = str(response.status_code)
                record_schedule_result(schedule_id, status=status, error=error_msg)
                return ScheduleDispatchResult(
                    schedule_id=schedule_id,
                    kind=kind,
                    status=status,
                    ref=ref,
                    error=error_msg,
                )

        error_msg = f"Unknown schedule kind: {kind}"
        record_schedule_result(schedule_id, status="error", error=error_msg)
        return ScheduleDispatchResult(
            schedule_id=schedule_id,
            kind=kind,
            status="error",
            error=error_msg,
        )

    except Exception as exc:
        logger.exception("Failed to dispatch schedule %s", schedule_id)
        error_str = str(exc)
        record_schedule_result(schedule_id, status="error", error=error_str)
        return ScheduleDispatchResult(
            schedule_id=schedule_id,
            kind=kind,
            status="error",
            error=error_str,
        )
    except asyncio.CancelledError:
        error_str = "Scheduler shutdown"
        record_schedule_result(schedule_id, status="error", error=error_str)
        raise


# Removed run_scheduler_tick in favor of concurrent task spawning in SchedulerService.tick


class SchedulerService:
    """Long-running scheduler that owns the tick loop and child tasks."""

    TICK_SECONDS_ENV = "AGENT_OS_SCHED_TICK_SECONDS"
    ENABLED_ENV = "AGENT_OS_SCHEDULER_ENABLED"

    def __init__(self) -> None:
        self._running = False
        self._loop_task: asyncio.Task[Any] | None = None
        self._child_tasks: set[asyncio.Task[Any]] = set()

    @property
    def enabled(self) -> bool:
        return os.environ.get(self.ENABLED_ENV, "true").lower() in (
            "true",
            "1",
            "yes",
            "on",
        )

    def _get_tick_seconds(self) -> float:
        try:
            val = float(os.environ.get(self.TICK_SECONDS_ENV, "1.0"))
            return max(0.1, val)
        except ValueError:
            return 1.0

    def start(self) -> None:
        """Create the tick loop task.  Must be called from a running loop."""
        if self._running:
            return
        if not self.enabled:
            logger.info("Scheduler disabled via %s", self.ENABLED_ENV)
            return
        self._running = True
        self._loop_task = asyncio.create_task(self._run_loop())

    async def _run_loop(self) -> None:
        tick_seconds = self._get_tick_seconds()
        while self._running:
            await self.tick()
            await asyncio.sleep(tick_seconds)

    async def tick(self, now: datetime | None = None) -> None:
        """Run one scheduler tick. Spawns child tasks for dispatch."""
        if not self._running:
            return

        capacity = max(0, 10 - len(self._child_tasks))
        if capacity <= 0:
            return

        if now is None:
            now = datetime.now(UTC)

        try:
            claimed = claim_due_schedules(now, limit=capacity)
            for schedule in claimed:
                task = asyncio.create_task(self._managed_dispatch(schedule))
                self._child_tasks.add(task)
                task.add_done_callback(self._child_tasks.discard)
        except Exception:
            logger.exception("Scheduler tick failed")

    async def _managed_dispatch(self, schedule: dict[str, Any]) -> None:
        try:
            result = await dispatch_schedule(schedule)
            if result.status == "error":
                logger.error(
                    "Schedule %s dispatch failed: %s",
                    result.schedule_id,
                    result.error,
                )
        except Exception:
            logger.exception("Failed to run schedule %s", schedule.get("schedule_id"))

    async def stop(self) -> None:
        """Stop claiming work and drain all already-dispatched tasks."""
        self._running = False

        if self._loop_task:
            self._loop_task.cancel()
            try:
                await self._loop_task
            except asyncio.CancelledError:
                pass
            self._loop_task = None

        if self._child_tasks:
            children = tuple(self._child_tasks)
            await asyncio.gather(*children, return_exceptions=True)
            self._child_tasks.clear()
