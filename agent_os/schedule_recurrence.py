"""Recurrence calculation for cron and interval schedule triggers.

This module owns parsing and next-fire-time computation.  It is pure
logic — no database, FastAPI, or graph imports — so it can be tested
in isolation from the rest of the scheduler.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Literal
from zoneinfo import ZoneInfo

from croniter import croniter

# ── Interval parsing ────────────────────────────────────────────────

_INTERVAL_RE = re.compile(
    r"^(?P<value>\d+)\s*(?P<unit>[smhd])?$",
    re.IGNORECASE,
)

_UNIT_SECONDS = {
    "s": 1,
    "m": 60,
    "h": 3600,
    "d": 86400,
}


def parse_interval(value: str) -> dt.timedelta:
    """Parse a human-friendly interval string into a *timedelta*.

    Accepted forms: ``30`` (seconds), ``30s``, ``5m``, ``2h``, ``1d``.
    Raises *ValueError* for non-positive or unparseable values.
    """
    m = _INTERVAL_RE.match(value.strip())
    if not m:
        raise ValueError(
            f"Invalid interval {value!r}; "
            "expected a positive integer with optional unit (s/m/h/d)"
        )
    num = int(m.group("value"))
    unit = (m.group("unit") or "s").lower()
    total_seconds = num * _UNIT_SECONDS[unit]
    if total_seconds <= 0:
        raise ValueError(f"Interval must be positive, got {value!r}")
    return dt.timedelta(seconds=total_seconds)


# ── Cron validation ─────────────────────────────────────────────────


def validate_cron(expression: str, timezone: str) -> None:
    """Raise *ValueError* if *expression* is not a valid 5-field cron or
    *timezone* is not a recognised IANA zone.
    """
    try:
        ZoneInfo(timezone)
    except (KeyError, ValueError) as exc:
        raise ValueError(f"Unknown timezone {timezone!r}") from exc

    parts = expression.strip().split()
    if len(parts) != 5:
        raise ValueError(
            f"Invalid cron expression {expression!r}: must have exactly 5 fields"
        )

    # croniter validates the expression on construction.
    try:
        croniter(expression)
    except (ValueError, KeyError) as exc:
        raise ValueError(f"Invalid cron expression {expression!r}: {exc}") from exc


# ── Next-fire-time calculation ──────────────────────────────────────


def next_run_at(
    trigger_kind: Literal["cron", "interval"],
    trigger_value: str,
    timezone: str,
    *,
    after: dt.datetime,
) -> dt.datetime:
    """Return the first occurrence **strictly after** *after*.

    For ``cron``, the occurrence is calculated in *timezone* and
    returned as an aware UTC datetime.  For ``interval``, *after* is
    simply advanced by the parsed interval (intervals are
    elapsed-time and ignore timezones).
    """
    if trigger_kind == "cron":
        tz = ZoneInfo(timezone)
        local_after = after.astimezone(tz)
        cron = croniter(trigger_value, local_after)
        next_local: dt.datetime = cron.get_next(dt.datetime)
        return next_local.astimezone(dt.UTC)

    if trigger_kind == "interval":
        delta = parse_interval(trigger_value)
        result = after + delta
        if result.tzinfo is None:
            result = result.replace(tzinfo=dt.UTC)
        return result

    raise ValueError(f"Unknown trigger_kind {trigger_kind!r}")


def coalesce_next_run(
    trigger_kind: Literal["cron", "interval"],
    trigger_value: str,
    timezone: str,
    *,
    persisted: dt.datetime,
    now: dt.datetime,
) -> dt.datetime:
    """Advance *persisted* until the result is strictly after *now*.

    This is the missed-fire coalescing helper: if multiple occurrences
    were missed, only one dispatch is created and the next-run time is
    set to the first future occurrence rather than generating a
    catch-up storm.
    """
    if persisted > now:
        return persisted

    if trigger_kind == "cron":
        return next_run_at(trigger_kind, trigger_value, timezone, after=now)

    if trigger_kind == "interval":
        delta = parse_interval(trigger_value)
        elapsed = now - persisted
        skips = elapsed // delta + 1
        return persisted + delta * skips

    raise ValueError(f"Unknown trigger_kind {trigger_kind!r}")
