"""Base SQLite connection utilities for Agent OS stores."""

from __future__ import annotations

import sqlite3
from typing import Any


def configure_sqlite_connection(
    conn: sqlite3.Connection,
    *,
    wal: bool = True,
    busy_timeout_ms: int = 30000,
    row_factory: Any = None,
) -> sqlite3.Connection:
    """Apply standard Agent OS WAL and busy timeout configuration to a SQLite connection."""
    if wal:
        conn.execute("PRAGMA journal_mode=WAL")
    if busy_timeout_ms > 0:
        conn.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")
    if row_factory is not None:
        conn.row_factory = row_factory
    return conn
