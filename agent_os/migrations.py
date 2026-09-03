"""Additive migration runner for AgentOS SQLite databases.

Enforces:
1. Strict additive-only changes (ADD COLUMN, CREATE TABLE IF NOT EXISTS, CREATE INDEX IF NOT EXISTS).
2. Version tracking via PRAGMA user_version.
3. Automatic backup to <db>.bak-<user_version> before migrations run.
4. Idempotency using PRAGMA table_info checks.
5. Fail-closed rollback on error.
"""

from __future__ import annotations

import contextlib
import logging
import re
import shutil
import sqlite3
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import NamedTuple

logger = logging.getLogger(__name__)

# Patterns for forbidden destructive operations
_FORBIDDEN_DDL_PATTERNS = [
    re.compile(r"\bDROP\s+TABLE\b", re.IGNORECASE),
    re.compile(r"\bDROP\s+COLUMN\b", re.IGNORECASE),
    re.compile(r"\bDROP\s+INDEX\b", re.IGNORECASE),
    re.compile(r"\bRENAME\s+TO\b", re.IGNORECASE),
    re.compile(r"\bRENAME\s+COLUMN\b", re.IGNORECASE),
    re.compile(r"\bTRUNCATE\b", re.IGNORECASE),
    re.compile(r"\bDELETE\s+FROM\b", re.IGNORECASE),
]

_ALTER_ADD_COLUMN_PATTERN = re.compile(
    r"ALTER\s+TABLE\s+([`\"\[]?\w+[`\"\]]?)\s+ADD\s+(?:COLUMN\s+)?([`\"\[]?\w+[`\"\]]?)\s+([^;]+)",
    re.IGNORECASE,
)


class MigrationError(RuntimeError):
    """Raised when a migration fails or violates safety invariants."""


class Migration(NamedTuple):
    """Auditable additive migration definition."""

    id: str
    version: int
    statements: tuple[str, ...] = ()
    handler: Callable[[sqlite3.Connection], None] | None = None
    description: str = ""


def _backfill_runs_workspace_id(conn: sqlite3.Connection) -> None:
    """Backfill runs.workspace_id from legacy runs.workspace."""
    conn.execute(
        """
        UPDATE runs
        SET workspace_id = workspace
        WHERE workspace_id IS NULL AND workspace IS NOT NULL
        """
    )


RUNS_MIGRATIONS: tuple[Migration, ...] = (
    Migration(
        id="0002_add_runs_principal_and_approvals",
        version=2,
        statements=(
            "ALTER TABLE runs ADD COLUMN workspace_id TEXT",
            "ALTER TABLE runs ADD COLUMN created_by TEXT",
            "ALTER TABLE runs ADD COLUMN created_by_kind TEXT",
            """
            CREATE TABLE IF NOT EXISTS run_approvals (
                run_id TEXT NOT NULL,
                seq INTEGER NOT NULL,
                decision TEXT NOT NULL,
                actor_id TEXT NOT NULL,
                actor_kind TEXT NOT NULL,
                on_behalf_of TEXT,
                reason TEXT,
                decided_at TEXT NOT NULL,
                PRIMARY KEY (run_id, seq)
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS run_approvals_run_id_idx
                ON run_approvals (run_id, seq ASC)
            """,
        ),
        handler=_backfill_runs_workspace_id,
        description="Add principal columns to runs and create run_approvals audit table",
    ),
    Migration(
        id="0003_add_runs_task_signature",
        version=3,
        statements=("ALTER TABLE runs ADD COLUMN task_signature TEXT",),
        description="Add client-provided opaque task signatures to runs",
    ),
)

SESSIONS_MIGRATIONS: tuple[Migration, ...] = (
    Migration(
        id="0002_add_sessions_workspace_and_principal",
        version=2,
        statements=(
            "ALTER TABLE sessions ADD COLUMN workspace_id TEXT",
            "ALTER TABLE sessions ADD COLUMN created_by TEXT",
        ),
        description="Add workspace_id and created_by to sessions",
    ),
)

PERMISSIONS_MIGRATIONS: tuple[Migration, ...] = (
    Migration(
        id="0002_add_permission_rules_workspace_and_taught_by",
        version=2,
        statements=(
            "ALTER TABLE permission_rules ADD COLUMN workspace_id TEXT",
            "ALTER TABLE permission_rules ADD COLUMN taught_by TEXT",
        ),
        description="Add workspace_id and taught_by to permission_rules",
    ),
)

OBSERVATIONS_MIGRATIONS: tuple[Migration, ...] = ()
SCHEDULES_MIGRATIONS: tuple[Migration, ...] = ()


def _is_safe_statement(sql: str) -> bool:
    """Ensure statement contains no destructive DDL or DML."""
    clean = sql.strip()
    if not clean:
        return True
    for pattern in _FORBIDDEN_DDL_PATTERNS:
        if pattern.search(clean):
            return False
    return True


def _table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    """Retrieve existing column names for a table."""
    clean_table = table_name.strip('`"[]')
    cursor = conn.execute(f"PRAGMA table_info({clean_table})")
    return {row[1] for row in cursor.fetchall()}


def _apply_statement(conn: sqlite3.Connection, sql: str) -> None:
    """Apply a single DDL statement idempotently."""
    clean = sql.strip()
    if not clean:
        return

    if not _is_safe_statement(clean):
        raise MigrationError(f"Disallowed destructive DDL detected: {clean}")

    # Check if this is an ADD COLUMN statement to guarantee idempotency
    match = _ALTER_ADD_COLUMN_PATTERN.match(clean)
    if match:
        table_name, col_name, _col_def = match.groups()
        table_name = table_name.strip('`"[]')
        col_name = col_name.strip('`"[]')
        existing_cols = _table_columns(conn, table_name)
        if col_name in existing_cols:
            logger.debug(
                "Column %s.%s already exists, skipping ADD COLUMN.",
                table_name,
                col_name,
            )
            return

    conn.execute(clean)


def backup_database(db_path: str | Path, version: int | None = None) -> Path | None:
    """Create a backup of the DB file before applying migrations.

    WAL-aware: Executes PRAGMA wal_checkpoint(TRUNCATE) before copying to flush
    all uncheckpointed frames into the main DB file. If checkpointing fails,
    falls back to copying the main file plus -wal and -shm files if present.

    Returns the backup Path or None if in-memory / nonexistent.
    """
    path_str = str(db_path)
    if path_str == ":memory:" or path_str.startswith("file::memory:"):
        return None

    path = Path(path_str).resolve()
    if not path.exists() or not path.is_file():
        return None

    if version is None:
        try:
            with contextlib.closing(sqlite3.connect(str(path), timeout=5.0)) as v_conn:
                row = v_conn.execute("PRAGMA user_version").fetchone()
                version = int(row[0]) if row else 1
        except Exception:
            version = 1

    backup_path = path.with_name(f"{path.name}.bak-{version}")

    checkpointed = False
    try:
        # closing() guarantees the connection is closed (a bare sqlite3 `with`
        # only manages the transaction, leaving the connection open until GC).
        with contextlib.closing(sqlite3.connect(str(path), timeout=5.0)) as chk_conn:
            chk_conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        checkpointed = True
    except Exception as exc:
        logger.warning(
            "WAL checkpoint failed for %s (%s); will fallback to copying sidecar files.",
            path,
            exc,
        )

    try:
        shutil.copy2(path, backup_path)
        if not checkpointed:
            wal_file = path.with_name(f"{path.name}-wal")
            shm_file = path.with_name(f"{path.name}-shm")
            if wal_file.exists():
                shutil.copy2(wal_file, backup_path.with_name(f"{backup_path.name}-wal"))
            if shm_file.exists():
                shutil.copy2(shm_file, backup_path.with_name(f"{backup_path.name}-shm"))

        logger.info("Created pre-migration backup at %s", backup_path)
        return backup_path
    except OSError as err:
        raise MigrationError(
            f"Failed to create pre-migration backup for {path}: {err}"
        ) from err


def run_migrations(
    db_path: str | Path | sqlite3.Connection,
    migrations: Sequence[Migration] = (),
    *,
    baseline_version: int = 1,
) -> list[str]:
    """Execute pending additive migrations against a database.

    Features:
    - Backup created before applying any pending migrations.
    - Idempotent and fail-closed: rollback on exception.
    - PRAGMA user_version updated sequentially.
    """
    is_conn = isinstance(db_path, sqlite3.Connection)
    conn = db_path if is_conn else sqlite3.connect(str(db_path))

    applied_ids: list[str] = []
    try:
        cursor = conn.execute("PRAGMA user_version")
        current_version = cursor.fetchone()[0]

        # Determine pending migrations
        pending = [m for m in migrations if m.version > current_version]
        pending.sort(key=lambda m: m.version)

        if not pending:
            # If database has no version set yet, set to baseline version
            if current_version == 0 and baseline_version > 0:
                conn.execute(f"PRAGMA user_version = {baseline_version}")
                conn.commit()
            return []

        # File backup before modifying
        if not is_conn:
            backup_database(db_path, current_version)

        # Apply pending migrations inside transaction
        conn.execute("BEGIN IMMEDIATE")
        try:
            for migration in pending:
                logger.info(
                    "Applying migration %s (v%d): %s",
                    migration.id,
                    migration.version,
                    migration.description,
                )
                for stmt in migration.statements:
                    _apply_statement(conn, stmt)

                if migration.handler is not None:
                    migration.handler(conn)

                conn.execute(f"PRAGMA user_version = {migration.version}")
                applied_ids.append(migration.id)

            conn.commit()
        except Exception as exc:
            conn.rollback()
            logger.error("Migration failed, rolled back: %s", exc)
            raise MigrationError(f"Migration execution failed: {exc}") from exc

        return applied_ids
    finally:
        if not is_conn:
            conn.close()
