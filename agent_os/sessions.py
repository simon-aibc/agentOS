import contextlib
import datetime as dt
import os
import sqlite3
from typing import Any

from agent_os.checkpoints import CHECKPOINT_DB_ENV, DEFAULT_CHECKPOINT_DB
from agent_os.migrations import SESSIONS_MIGRATIONS, run_migrations
from agent_os.stores.base import configure_sqlite_connection


def _get_db_path() -> str:
    return os.getenv(CHECKPOINT_DB_ENV, DEFAULT_CHECKPOINT_DB)


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_get_db_path(), timeout=30.0)
    return configure_sqlite_connection(conn, wal=True, busy_timeout_ms=30000)


def _init_db() -> None:
    path = _get_db_path()
    with contextlib.closing(_connect()) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                thread_id TEXT PRIMARY KEY,
                created_at TEXT,
                last_turn_at TEXT,
                turn_count INTEGER,
                title TEXT
            )
        """)
        conn.commit()
    run_migrations(path, SESSIONS_MIGRATIONS, baseline_version=1)


def upsert_session(
    thread_id: str,
    title: str | None = None,
    *,
    workspace_id: str | None = None,
    created_by: str | None = None,
) -> None:
    _init_db()
    now = dt.datetime.now(dt.UTC).isoformat()
    with contextlib.closing(_connect()) as conn:
        cursor = conn.execute(
            "SELECT created_at, turn_count, title, workspace_id, created_by FROM sessions WHERE thread_id = ?",
            (thread_id,),
        )
        row = cursor.fetchone()
        if row:
            _, turn_count, existing_title, existing_ws, existing_creator = row
            new_title = title if title is not None else existing_title
            new_ws = workspace_id if workspace_id is not None else existing_ws
            new_creator = created_by if created_by is not None else existing_creator
            conn.execute(
                """
                UPDATE sessions 
                SET last_turn_at = ?, turn_count = ?, title = ?, workspace_id = ?, created_by = ?
                WHERE thread_id = ?
            """,
                (now, turn_count + 1, new_title, new_ws, new_creator, thread_id),
            )
        else:
            conn.execute(
                """
                INSERT INTO sessions (
                    thread_id, created_at, last_turn_at, turn_count, title, workspace_id, created_by
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
                (thread_id, now, now, 1, title or "New Session", workspace_id, created_by),
            )
        conn.commit()


def list_sessions() -> list[dict[str, Any]]:
    _init_db()
    with contextlib.closing(_connect()) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.execute("SELECT * FROM sessions ORDER BY last_turn_at DESC")
        return [dict(row) for row in cursor.fetchall()]


def get_session(thread_id: str) -> dict[str, Any] | None:
    _init_db()
    with contextlib.closing(_connect()) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.execute(
            "SELECT * FROM sessions WHERE thread_id = ?", (thread_id,)
        )
        row = cursor.fetchone()
        return dict(row) if row else None


def delete_session(thread_id: str) -> None:
    _init_db()
    with contextlib.closing(_connect()) as conn:
        conn.execute("DELETE FROM sessions WHERE thread_id = ?", (thread_id,))
        conn.commit()
