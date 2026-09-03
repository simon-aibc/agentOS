"""Durable schedule store backed by its own SQLite file.

This module mirrors the proven separate-file pattern in
``agent_os/runs.py``.  The scheduler database is **physically
separate** from both the LangGraph checkpoint DB and the run ledger.

Default path derivation from ``AGENT_OS_CHECKPOINTS_DB``::

    checkpoints.db      →  checkpoints.sched.db
    state.sqlite        →  state.sched.sqlite
    /data/checkpoints   →  /data/checkpoints.sched.db

Override: ``AGENT_OS_SCHED_DB``.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import logging
import os
import sqlite3
import uuid
from typing import Any, Literal
from urllib.parse import urlsplit

from agent_os.checkpoints import CHECKPOINT_DB_ENV, DEFAULT_CHECKPOINT_DB
from agent_os.migrations import run_migrations
from agent_os.schedule_recurrence import (
    coalesce_next_run,
    next_run_at,
    parse_interval,
    validate_cron,
)
from agent_os.stores.base import configure_sqlite_connection

logger = logging.getLogger(__name__)

# ── Public constants ────────────────────────────────────────────────

SCHED_DB_ENV = "AGENT_OS_SCHED_DB"

ALLOWED_KINDS: frozenset[str] = frozenset({"run", "brief", "app_callback"})
ALLOWED_TRIGGER_KINDS: frozenset[str] = frozenset({"cron", "interval"})

# Valid payload keys per kind.
_RUN_PAYLOAD_KEYS: frozenset[str] = frozenset({"task", "workspace"})
_BRIEF_PAYLOAD_KEYS: frozenset[str] = frozenset()
_APP_CALLBACK_PAYLOAD_KEYS: frozenset[str] = frozenset(
    {"url", "method", "headers", "body"}
)
ALLOWED_HTTP_METHODS: frozenset[str] = frozenset(
    {"GET", "POST", "PUT", "PATCH", "DELETE"}
)

# ── DB path derivation (mirrors runs.py) ────────────────────────────


def _get_db_path() -> str:
    """Path to the scheduler SQLite file.

    Derived exactly as ``agent_os/runs.py::_get_db_path()`` but with
    ``.sched`` instead of ``.runs``.  Override with
    ``AGENT_OS_SCHED_DB``.
    """
    override = os.getenv(SCHED_DB_ENV)
    if override:
        return override
    checkpoint_path = os.getenv(CHECKPOINT_DB_ENV, DEFAULT_CHECKPOINT_DB)
    if checkpoint_path == ":memory:":
        return checkpoint_path
    root, ext = os.path.splitext(checkpoint_path)
    return f"{root}.sched{ext or '.db'}"


def _connect() -> sqlite3.Connection:
    """Open the scheduler DB with WAL + busy_timeout=30 000 ms."""
    conn = sqlite3.connect(_get_db_path(), timeout=30.0)
    return configure_sqlite_connection(conn, wal=True, busy_timeout_ms=30000)


def _init_schedules_db() -> None:
    """Idempotently create the schedules table and index, migrating if needed."""
    with contextlib.closing(_connect()) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            cursor = conn.execute(
                "SELECT sql FROM sqlite_master "
                "WHERE type = 'table' AND name = 'schedules'"
            )
            row = cursor.fetchone()
            if row is None:
                conn.execute("""
                    CREATE TABLE schedules (
                        schedule_id TEXT PRIMARY KEY,
                        name TEXT NOT NULL,
                        kind TEXT NOT NULL CHECK (kind IN ('run', 'brief', 'app_callback')),
                        trigger_kind TEXT NOT NULL
                            CHECK (trigger_kind IN ('cron', 'interval')),
                        trigger_value TEXT NOT NULL,
                        timezone TEXT NOT NULL,
                        payload TEXT NOT NULL,
                        enabled INTEGER NOT NULL DEFAULT 1,
                        next_run_at TEXT NOT NULL,
                        last_run_at TEXT,
                        last_status TEXT,
                        last_error TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    )
                """)
            else:
                table_sql = row[0] or ""
                if "app_callback" not in table_sql:
                    index_rows = conn.execute(
                        "SELECT sql FROM sqlite_master "
                        "WHERE type = 'index' AND tbl_name = 'schedules' "
                        "AND sql IS NOT NULL ORDER BY name"
                    ).fetchall()
                    index_sql = [index_row[0] for index_row in index_rows]

                    conn.execute("""
                        CREATE TABLE schedules__migration (
                            schedule_id TEXT PRIMARY KEY,
                            name TEXT NOT NULL,
                            kind TEXT NOT NULL CHECK (kind IN ('run', 'brief', 'app_callback')),
                            trigger_kind TEXT NOT NULL
                                CHECK (trigger_kind IN ('cron', 'interval')),
                            trigger_value TEXT NOT NULL,
                            timezone TEXT NOT NULL,
                            payload TEXT NOT NULL,
                            enabled INTEGER NOT NULL DEFAULT 1,
                            next_run_at TEXT NOT NULL,
                            last_run_at TEXT,
                            last_status TEXT,
                            last_error TEXT,
                            created_at TEXT NOT NULL,
                            updated_at TEXT NOT NULL
                        )
                    """)
                    conn.execute("""
                        INSERT INTO schedules__migration (
                            schedule_id, name, kind, trigger_kind, trigger_value,
                            timezone, payload, enabled, next_run_at, last_run_at,
                            last_status, last_error, created_at, updated_at
                        )
                        SELECT
                            schedule_id, name, kind, trigger_kind, trigger_value,
                            timezone, payload, enabled, next_run_at, last_run_at,
                            last_status, last_error, created_at, updated_at
                        FROM schedules
                    """)
                    conn.execute("DROP TABLE schedules")
                    conn.execute("ALTER TABLE schedules__migration RENAME TO schedules")
                    for statement in index_sql:
                        conn.execute(statement)

            conn.execute("""
                CREATE INDEX IF NOT EXISTS schedules_due_idx
                    ON schedules(enabled, next_run_at)
            """)
            run_migrations(conn, baseline_version=1)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise


# ── Validation helpers ──────────────────────────────────────────────


def validate_app_callback_url(value: Any) -> str:
    """Return a stripped HTTP(S) URL or raise ``ValueError``."""
    message = "url must start with 'http://' or 'https://' and include a valid host"
    if not isinstance(value, str):
        raise ValueError(message)

    value = value.strip()
    try:
        parsed = urlsplit(value)
        # Accessing port validates malformed and out-of-range port values.
        _ = parsed.port
    except ValueError as exc:
        raise ValueError(message) from exc

    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.hostname
        or any(character.isspace() for character in value)
    ):
        raise ValueError(message)
    return value


def _validate_schedule_fields(
    *,
    kind: str,
    trigger_kind: str,
    trigger_value: str,
    timezone: str,
    payload: dict[str, Any],
) -> None:
    """Raise *ValueError* for invalid combinations."""
    if kind not in ALLOWED_KINDS:
        raise ValueError(f"kind must be one of {sorted(ALLOWED_KINDS)}, got {kind!r}")
    if trigger_kind not in ALLOWED_TRIGGER_KINDS:
        raise ValueError(
            f"trigger_kind must be one of {sorted(ALLOWED_TRIGGER_KINDS)}, "
            f"got {trigger_kind!r}"
        )

    # Trigger-specific validation.
    if trigger_kind == "cron":
        validate_cron(trigger_value, timezone)
    else:
        parse_interval(trigger_value)
        if timezone != "UTC":
            raise ValueError(
                "Interval schedules are elapsed-time and reject "
                f"non-UTC timezone; got {timezone!r}"
            )

    # Payload validation per kind — reject unknown keys.
    if kind == "run":
        unknown = set(payload) - _RUN_PAYLOAD_KEYS
        if unknown:
            raise ValueError(f"Unknown payload keys for kind=run: {sorted(unknown)}")
        if "task" not in payload:
            raise ValueError("kind=run requires 'task' in payload")
    elif kind == "brief":
        unknown = set(payload) - _BRIEF_PAYLOAD_KEYS
        if unknown:
            raise ValueError(f"Unknown payload keys for kind=brief: {sorted(unknown)}")
    elif kind == "app_callback":
        unknown = set(payload) - _APP_CALLBACK_PAYLOAD_KEYS
        if unknown:
            raise ValueError(
                f"Unknown payload keys for kind=app_callback: {sorted(unknown)}"
            )
        if "url" not in payload or not payload["url"]:
            raise ValueError("kind=app_callback requires 'url' in payload")
        payload["url"] = validate_app_callback_url(payload["url"])
        method = payload.get("method") or "POST"
        if not isinstance(method, str) or method.upper() not in ALLOWED_HTTP_METHODS:
            raise ValueError(
                f"method must be one of {sorted(ALLOWED_HTTP_METHODS)}, got {method!r}"
            )
        payload["method"] = method.upper()
        if "headers" in payload and payload["headers"] is not None:
            if not isinstance(payload["headers"], dict) or not all(
                isinstance(k, str) and isinstance(v, str)
                for k, v in payload["headers"].items()
            ):
                raise ValueError("headers must be a dict of string key-value pairs")
        if "body" in payload and payload["body"] is not None:
            if not isinstance(payload["body"], dict):
                raise ValueError("body must be a dict")


# ── CRUD ────────────────────────────────────────────────────────────


def create_schedule(
    *,
    name: str,
    kind: Literal["run", "brief", "app_callback"],
    trigger_kind: Literal["cron", "interval"],
    trigger_value: str,
    timezone: str = "UTC",
    payload: dict[str, Any] | None = None,
    enabled: bool = True,
) -> str:
    """Persist a new schedule, returning its *schedule_id*.

    Validates all fields before touching the database.
    """
    payload = dict(payload or {})
    _validate_schedule_fields(
        kind=kind,
        trigger_kind=trigger_kind,
        trigger_value=trigger_value,
        timezone=timezone,
        payload=payload,
    )

    _init_schedules_db()
    schedule_id = str(uuid.uuid4())
    now = dt.datetime.now(dt.UTC)
    computed_next = next_run_at(trigger_kind, trigger_value, timezone, after=now)
    now_iso = now.isoformat()
    next_iso = computed_next.isoformat()

    with contextlib.closing(_connect()) as conn:
        conn.execute(
            """
            INSERT INTO schedules (
                schedule_id, name, kind, trigger_kind, trigger_value,
                timezone, payload, enabled,
                next_run_at, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                schedule_id,
                name,
                kind,
                trigger_kind,
                trigger_value,
                timezone,
                json.dumps(payload),
                int(enabled),
                next_iso,
                now_iso,
                now_iso,
            ),
        )
        conn.commit()
    return schedule_id


def get_schedule(schedule_id: str) -> dict[str, Any] | None:
    """Fetch a single schedule by ID, or *None*."""
    _init_schedules_db()
    with contextlib.closing(_connect()) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.execute(
            "SELECT * FROM schedules WHERE schedule_id = ?",
            (schedule_id,),
        )
        row = cursor.fetchone()
        return _decode_row(row) if row else None


def list_schedules(
    *,
    kind: str | None = None,
    enabled: bool | None = None,
) -> list[dict[str, Any]]:
    """Return schedules ordered by ``created_at DESC``."""
    _init_schedules_db()
    where: list[str] = []
    params: list[Any] = []
    if kind is not None:
        where.append("kind = ?")
        params.append(kind)
    if enabled is not None:
        where.append("enabled = ?")
        params.append(int(enabled))

    query = "SELECT * FROM schedules"
    if where:
        query += " WHERE " + " AND ".join(where)
    query += " ORDER BY created_at DESC"

    with contextlib.closing(_connect()) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.execute(query, params)
        return [_decode_row(row) for row in cursor.fetchall()]


def remove_schedule(schedule_id: str) -> bool:
    """Delete a schedule.  Returns *True* if a row was removed."""
    _init_schedules_db()
    with contextlib.closing(_connect()) as conn:
        cursor = conn.execute(
            "DELETE FROM schedules WHERE schedule_id = ?",
            (schedule_id,),
        )
        conn.commit()
        return cursor.rowcount > 0


def set_schedule_enabled(schedule_id: str, enabled: bool) -> bool:
    """Toggle a schedule's enabled state.  Returns *True* if the row existed."""
    _init_schedules_db()
    now_iso = dt.datetime.now(dt.UTC).isoformat()
    with contextlib.closing(_connect()) as conn:
        cursor = conn.execute(
            """
            UPDATE schedules SET enabled = ?, updated_at = ?
            WHERE schedule_id = ?
            """,
            (int(enabled), now_iso, schedule_id),
        )
        conn.commit()
        return cursor.rowcount > 0


# ── Atomic due-claim ────────────────────────────────────────────────


def claim_due_schedules(
    now: dt.datetime,
    *,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Atomically claim up to *limit* due schedules.

    For each due row the ``next_run_at`` is advanced (with
    missed-fire coalescing) inside one ``BEGIN IMMEDIATE``
    transaction, preventing two local ``serve`` processes from
    dispatching the same occurrence.
    """
    _init_schedules_db()
    now_iso = now.isoformat()
    claimed: list[dict[str, Any]] = []

    with contextlib.closing(_connect()) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.execute(
            """
            SELECT * FROM schedules
            WHERE enabled = 1 AND next_run_at <= ?
            ORDER BY next_run_at ASC
            LIMIT ?
            """,
            (now_iso, limit),
        )
        rows = cursor.fetchall()

        for row in rows:
            decoded = _decode_row(row)
            # Advance next_run_at, coalescing any missed fires.
            new_next = coalesce_next_run(
                decoded["trigger_kind"],
                decoded["trigger_value"],
                decoded["timezone"],
                persisted=dt.datetime.fromisoformat(decoded["next_run_at"]),
                now=now,
            )
            conn.execute(
                """
                UPDATE schedules
                SET next_run_at = ?, last_run_at = ?, updated_at = ?
                WHERE schedule_id = ?
                """,
                (
                    new_next.isoformat(),
                    now_iso,
                    now_iso,
                    decoded["schedule_id"],
                ),
            )
            claimed.append(decoded)

        conn.commit()

    return claimed


# ── Result recording ────────────────────────────────────────────────


def record_schedule_result(
    schedule_id: str,
    *,
    status: str,
    error: str | None = None,
) -> None:
    """Update last_status and last_error for a schedule."""
    _init_schedules_db()
    now_iso = dt.datetime.now(dt.UTC).isoformat()
    with contextlib.closing(_connect()) as conn:
        conn.execute(
            """
            UPDATE schedules
            SET last_status = ?, last_error = ?, updated_at = ?
            WHERE schedule_id = ?
            """,
            (status, error, now_iso, schedule_id),
        )
        conn.commit()


# ── Row decoding ────────────────────────────────────────────────────


def _decode_row(row: sqlite3.Row) -> dict[str, Any]:
    """Convert a raw row to a dict, decoding payload JSON."""
    d = dict(row)
    d["payload"] = json.loads(d["payload"])
    d["enabled"] = bool(d["enabled"])
    return d
