import contextlib
import datetime as dt
import json
import os
import sqlite3
import uuid
from typing import Any

from agent_os.checkpoints import CHECKPOINT_DB_ENV, DEFAULT_CHECKPOINT_DB
from agent_os.migrations import RUNS_MIGRATIONS, run_migrations
from agent_os.principal import Principal
from agent_os.stores.base import configure_sqlite_connection

RUNS_DB_ENV = "AGENT_OS_RUNS_DB"
TERMINAL_RUN_STATUSES = frozenset({"completed", "error", "cancelled"})


def _get_db_path() -> str:
    """Path to the run-ledger SQLite file.

    The ledger lives in its OWN file, not the LangGraph checkpoint DB: a
    synchronous ledger writer and the async checkpointer writing the same file
    on the same event loop deadlock (the blocking ledger call freezes the loop
    that would release the lock). By default the ledger file is derived next to
    the checkpoint DB (``checkpoints.db`` -> ``checkpoints.runs.db``); override
    with AGENT_OS_RUNS_DB.
    """
    override = os.getenv(RUNS_DB_ENV)
    if override:
        return override
    checkpoint_path = os.getenv(CHECKPOINT_DB_ENV, DEFAULT_CHECKPOINT_DB)
    if checkpoint_path == ":memory:":
        return checkpoint_path
    root, ext = os.path.splitext(checkpoint_path)
    return f"{root}.runs{ext or '.db'}"


def _connect() -> sqlite3.Connection:
    """Open the ledger DB with WAL + busy_timeout so the synchronous ledger
    writer does not deadlock with the async LangGraph checkpointer sharing the
    same file (both write concurrently during a run)."""
    conn = sqlite3.connect(_get_db_path(), timeout=30.0)
    return configure_sqlite_connection(conn, wal=True, busy_timeout_ms=30000)


def _init_runs_db() -> None:
    db_path = _get_db_path()
    with contextlib.closing(_connect()) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY,
                thread_id TEXT NOT NULL,
                workspace TEXT,
                status TEXT NOT NULL,
                task TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                ended_at TEXT,
                error TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS run_events (
                run_id TEXT NOT NULL,
                seq INTEGER NOT NULL,
                ts TEXT NOT NULL,
                kind TEXT NOT NULL,
                payload TEXT NOT NULL,
                PRIMARY KEY (run_id, seq)
            )
        """)
        conn.commit()
    run_migrations(db_path, RUNS_MIGRATIONS, baseline_version=1)


def create_run(
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
    _init_runs_db()
    run_id = str(uuid.uuid4())
    now = dt.datetime.now(dt.UTC).isoformat()

    if principal is not None:
        created_by = principal.id
        created_by_kind = principal.kind

    eff_workspace_id = workspace_id if workspace_id is not None else workspace
    eff_workspace = workspace if workspace is not None else workspace_id

    with contextlib.closing(_connect()) as conn:
        conn.execute(
            """
            INSERT INTO runs (
                run_id, thread_id, workspace, workspace_id, status, task, task_signature,
                created_by, created_by_kind, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                thread_id,
                eff_workspace,
                eff_workspace_id,
                "queued",
                task,
                task_signature,
                created_by,
                created_by_kind,
                now,
                now,
            ),
        )
        conn.commit()
    return run_id


def append_event(run_id: str, kind: str, payload: dict[str, Any]) -> int:
    _init_runs_db()
    ts = dt.datetime.now(dt.UTC).isoformat()
    serialized_payload = json.dumps(payload)
    with contextlib.closing(_connect()) as conn:
        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 FROM run_events WHERE run_id = ?",
            (run_id,),
        )
        seq = int(cursor.fetchone()[0])
        conn.execute(
            """
            INSERT INTO run_events (run_id, seq, ts, kind, payload)
            VALUES (?, ?, ?, ?, ?)
            """,
            (run_id, seq, ts, kind, serialized_payload),
        )
        conn.commit()
    return seq


def set_status(
    run_id: str,
    status: str,
    *,
    error: str | None = None,
    ended: bool = False,
) -> None:
    _init_runs_db()
    now = dt.datetime.now(dt.UTC).isoformat()
    ended_at = now if ended else None
    with contextlib.closing(_connect()) as conn:
        if ended:
            conn.execute(
                """
                UPDATE runs
                SET status = ?, updated_at = ?, ended_at = ?, error = ?
                WHERE run_id = ?
                """,
                (status, now, ended_at, error, run_id),
            )
        else:
            conn.execute(
                """
                UPDATE runs
                SET status = ?, updated_at = ?, error = ?
                WHERE run_id = ?
                """,
                (status, now, error, run_id),
            )
        conn.commit()


def transition_status(
    run_id: str,
    *,
    expected: str,
    status: str,
    error: str | None = None,
    ended: bool = False,
) -> bool:
    _init_runs_db()
    now = dt.datetime.now(dt.UTC).isoformat()
    ended_at = now if ended else None
    with contextlib.closing(_connect()) as conn:
        if ended:
            cursor = conn.execute(
                """
                UPDATE runs
                SET status = ?, updated_at = ?, ended_at = ?, error = ?
                WHERE run_id = ? AND status = ?
                """,
                (status, now, ended_at, error, run_id, expected),
            )
        else:
            cursor = conn.execute(
                """
                UPDATE runs
                SET status = ?, updated_at = ?, error = ?
                WHERE run_id = ? AND status = ?
                """,
                (status, now, error, run_id, expected),
            )
        conn.commit()
        return cursor.rowcount == 1


def record_approval(
    run_id: str,
    *,
    decision: str,
    actor_id: str,
    actor_kind: str,
    on_behalf_of: str | None = None,
    reason: str | None = None,
) -> int:
    """Record an audit decision for a run into the run_approvals ledger."""
    _init_runs_db()
    decided_at = dt.datetime.now(dt.UTC).isoformat()
    with contextlib.closing(_connect()) as conn:
        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.execute("SELECT 1 FROM runs WHERE run_id = ?", (run_id,))
        if cursor.fetchone() is None:
            conn.rollback()
            raise KeyError(f"Run '{run_id}' not found")
        seq_cursor = conn.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 FROM run_approvals WHERE run_id = ?",
            (run_id,),
        )
        seq = int(seq_cursor.fetchone()[0])
        conn.execute(
            """
            INSERT INTO run_approvals (
                run_id, seq, decision, actor_id, actor_kind, on_behalf_of, reason, decided_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (run_id, seq, decision, actor_id, actor_kind, on_behalf_of, reason, decided_at),
        )
        conn.commit()
    return seq


def record_approval_and_transition(
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
    """Atomically write an audit decision and transition run status if expected status matches."""
    _init_runs_db()
    now = dt.datetime.now(dt.UTC).isoformat()
    ended_at = now if ended else None
    with contextlib.closing(_connect()) as conn:
        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.execute("SELECT status FROM runs WHERE run_id = ?", (run_id,))
        row = cursor.fetchone()
        if row is None or row[0] != expected:
            conn.rollback()
            return False

        seq_cursor = conn.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 FROM run_approvals WHERE run_id = ?",
            (run_id,),
        )
        seq = int(seq_cursor.fetchone()[0])
        conn.execute(
            """
            INSERT INTO run_approvals (
                run_id, seq, decision, actor_id, actor_kind, on_behalf_of, reason, decided_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (run_id, seq, decision, actor_id, actor_kind, on_behalf_of, reason, now),
        )
        if ended:
            conn.execute(
                """
                UPDATE runs
                SET status = ?, updated_at = ?, ended_at = ?, error = ?
                WHERE run_id = ? AND status = ?
                """,
                (status, now, ended_at, error, run_id, expected),
            )
        else:
            conn.execute(
                """
                UPDATE runs
                SET status = ?, updated_at = ?, error = ?
                WHERE run_id = ? AND status = ?
                """,
                (status, now, error, run_id, expected),
            )
        conn.commit()
        return True


def record_cancellation_and_transition(
    run_id: str,
    *,
    actor_id: str,
    actor_kind: str,
    on_behalf_of: str | None = None,
    reason: str | None = None,
) -> bool:
    """Atomically record cancellation audit and transition non-terminal run to 'cancelled'.

    Returns False if the run does not exist or is already in a terminal status.
    """
    _init_runs_db()
    now = dt.datetime.now(dt.UTC).isoformat()
    with contextlib.closing(_connect()) as conn:
        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.execute("SELECT status FROM runs WHERE run_id = ?", (run_id,))
        row = cursor.fetchone()
        if row is None:
            conn.rollback()
            return False
        current_status = row[0]
        if current_status in TERMINAL_RUN_STATUSES:
            conn.rollback()
            return False

        seq_cursor = conn.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 FROM run_approvals WHERE run_id = ?",
            (run_id,),
        )
        seq = int(seq_cursor.fetchone()[0])
        conn.execute(
            """
            INSERT INTO run_approvals (
                run_id, seq, decision, actor_id, actor_kind, on_behalf_of, reason, decided_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (run_id, seq, "cancelled", actor_id, actor_kind, on_behalf_of, reason, now),
        )
        update_cursor = conn.execute(
            """
            UPDATE runs
            SET status = 'cancelled', updated_at = ?, ended_at = ?
            WHERE run_id = ? AND status = ?
            """,
            (now, now, run_id, current_status),
        )
        if update_cursor.rowcount != 1:
            conn.rollback()
            return False

        conn.commit()
        return True


def list_approvals(run_id: str) -> list[dict[str, Any]]:
    """Return all recorded audit decisions for a run ordered by sequence number."""
    _init_runs_db()
    with contextlib.closing(_connect()) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.execute(
            """
            SELECT run_id, seq, decision, actor_id, actor_kind, on_behalf_of, reason, decided_at
            FROM run_approvals
            WHERE run_id = ?
            ORDER BY seq ASC
            """,
            (run_id,),
        )
        return [dict(row) for row in cursor.fetchall()]


def get_run(run_id: str) -> dict[str, Any] | None:
    _init_runs_db()
    with contextlib.closing(_connect()) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,))
        row = cursor.fetchone()
        return dict(row) if row else None


def list_runs(
    *,
    status: str | None = None,
    workspace: str | None = None,
    workspace_id: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    _init_runs_db()
    where = []
    params: list[Any] = []
    if status is not None:
        where.append("status = ?")
        params.append(status)
    target_workspace = workspace_id if workspace_id is not None else workspace
    if target_workspace is not None:
        where.append("(workspace = ? OR workspace_id = ?)")
        params.extend([target_workspace, target_workspace])

    query = "SELECT * FROM runs"
    if where:
        query += " WHERE " + " AND ".join(where)
    query += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)

    with contextlib.closing(_connect()) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.execute(query, params)
        return [dict(row) for row in cursor.fetchall()]


def list_events(run_id: str, *, after: int = 0) -> list[dict[str, Any]]:
    _init_runs_db()
    with contextlib.closing(_connect()) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.execute(
            """
            SELECT * FROM run_events
            WHERE run_id = ? AND seq > ?
            ORDER BY seq ASC
            """,
            (run_id, after),
        )
        events = []
        for row in cursor.fetchall():
            event = dict(row)
            event["payload"] = json.loads(event["payload"])
            events.append(event)
        return events
