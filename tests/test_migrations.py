import sqlite3
from contextlib import contextmanager
from pathlib import Path

import pytest

from agent_os.migrations import (
    Migration,
    MigrationError,
    _is_safe_statement,
    backup_database,
    run_migrations,
)


@contextmanager
def _sqlite_connection(path: str | Path):
    conn = sqlite3.connect(path)
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def test_is_safe_statement():
    assert _is_safe_statement("ALTER TABLE users ADD COLUMN age INTEGER")
    assert _is_safe_statement("CREATE TABLE IF NOT EXISTS items (id TEXT)")
    assert _is_safe_statement("CREATE INDEX IF NOT EXISTS idx_items ON items (id)")

    # Destructive statements must be rejected
    assert not _is_safe_statement("DROP TABLE users")
    assert not _is_safe_statement("ALTER TABLE users DROP COLUMN age")
    assert not _is_safe_statement("DROP INDEX idx_items")
    assert not _is_safe_statement("RENAME TO new_table")
    assert not _is_safe_statement("DELETE FROM users")
    assert not _is_safe_statement("TRUNCATE TABLE users")


def test_migrations_fresh_db_sets_baseline(tmp_path: Path):
    db_file = tmp_path / "test.db"

    applied = run_migrations(db_file, baseline_version=1)
    assert applied == []

    with _sqlite_connection(db_file) as conn:
        ver = conn.execute("PRAGMA user_version").fetchone()[0]
        assert ver == 1


def test_migrations_apply_and_idempotent(tmp_path: Path):
    db_file = tmp_path / "app.db"

    # Initial setup
    with _sqlite_connection(db_file) as conn:
        conn.execute("CREATE TABLE users (id TEXT PRIMARY KEY, name TEXT)")
        conn.execute("INSERT INTO users VALUES ('u1', 'Alice')")
        conn.execute("PRAGMA user_version = 1")

    migrations = [
        Migration(
            id="0002_add_email",
            version=2,
            statements=("ALTER TABLE users ADD COLUMN email TEXT DEFAULT 'none'",),
            description="Add email column",
        ),
        Migration(
            id="0003_add_index",
            version=3,
            statements=("CREATE INDEX IF NOT EXISTS idx_users_name ON users(name)",),
            description="Add index on name",
        ),
    ]

    # Run migrations once
    applied = run_migrations(db_file, migrations)
    assert applied == ["0002_add_email", "0003_add_index"]

    # Check backup created
    backup_file = tmp_path / "app.db.bak-1"
    assert backup_file.exists()

    # Check schema updated and data intact
    with _sqlite_connection(db_file) as conn:
        ver = conn.execute("PRAGMA user_version").fetchone()[0]
        assert ver == 3
        row = conn.execute("SELECT id, name, email FROM users WHERE id='u1'").fetchone()
        assert row == ("u1", "Alice", "none")

    # Run again -> should be no-op (idempotent)
    applied_again = run_migrations(db_file, migrations)
    assert applied_again == []


def test_migration_fail_closed_rollback(tmp_path: Path):
    db_file = tmp_path / "fail.db"

    with _sqlite_connection(db_file) as conn:
        conn.execute("CREATE TABLE accounts (id TEXT PRIMARY KEY, balance REAL)")
        conn.execute("INSERT INTO accounts VALUES ('acc1', 100.0)")
        conn.execute("PRAGMA user_version = 1")

    bad_migrations = [
        Migration(
            id="0002_add_currency",
            version=2,
            statements=("ALTER TABLE accounts ADD COLUMN currency TEXT DEFAULT 'USD'",),
        ),
        Migration(
            id="0003_broken_migration",
            version=3,
            statements=("SYNTAX ERROR IS NOT VALID SQL",),
        ),
    ]

    with pytest.raises(MigrationError):
        run_migrations(db_file, bad_migrations)

    # Verify rollback: version remains 1, column currency was rolled back
    with _sqlite_connection(db_file) as conn:
        ver = conn.execute("PRAGMA user_version").fetchone()[0]
        assert ver == 1
        cols = {r[1] for r in conn.execute("PRAGMA table_info(accounts)").fetchall()}
        assert "currency" not in cols
        row = conn.execute("SELECT id, balance FROM accounts").fetchone()
        assert row == ("acc1", 100.0)

    # Verify backup exists
    assert (tmp_path / "fail.db.bak-1").exists()


def test_migration_disallows_destructive_ddl(tmp_path: Path):
    db_file = tmp_path / "destructive.db"
    with _sqlite_connection(db_file) as conn:
        conn.execute("CREATE TABLE secret (data TEXT)")
        conn.execute("PRAGMA user_version = 1")

    destructive = [
        Migration(
            id="0002_drop_table",
            version=2,
            statements=("DROP TABLE secret",),
        )
    ]

    with pytest.raises(MigrationError, match="Disallowed destructive DDL"):
        run_migrations(db_file, destructive)


def test_migration_in_memory_db():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE test (id INT)")
    conn.execute("PRAGMA user_version = 0")

    migrations = [
        Migration(
            id="0001_add_col",
            version=1,
            statements=("ALTER TABLE test ADD COLUMN name TEXT",),
        )
    ]

    applied = run_migrations(conn, migrations)
    assert applied == ["0001_add_col"]
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
    conn.close()


def test_backup_database_wal_checkpoint(tmp_path: Path):
    db_file = tmp_path / "wal_test.db"

    # Setup WAL database
    with _sqlite_connection(db_file) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE records (id INT, val TEXT)")
        conn.execute("INSERT INTO records VALUES (1, 'alpha'), (2, 'beta')")
        conn.commit()

    # Insert more records without explicit checkpoint
    with _sqlite_connection(db_file) as conn:
        conn.execute("INSERT INTO records VALUES (3, 'gamma')")
        conn.commit()

    # Backup the database
    backup_file = backup_database(db_file, version=1)
    assert backup_file is not None
    assert backup_file.exists()

    # Read from backup file independently
    with _sqlite_connection(backup_file) as b_conn:
        rows = b_conn.execute("SELECT id, val FROM records ORDER BY id").fetchall()
        assert rows == [(1, "alpha"), (2, "beta"), (3, "gamma")]


def test_backup_database_memory_and_nonexistent(tmp_path: Path):
    assert backup_database(":memory:", 1) is None
    assert backup_database("file::memory:?cache=shared", 1) is None
    assert backup_database(tmp_path / "nonexistent.db", 1) is None


def test_runs_migration_v1_to_v2_with_backfill_and_backup(tmp_path: Path):
    from agent_os.migrations import RUNS_MIGRATIONS

    db_file = tmp_path / "runs_test.db"

    # 1. Create real pre-Phase-0 v1 schema
    with _sqlite_connection(db_file) as conn:
        conn.execute("""
            CREATE TABLE runs (
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
            CREATE TABLE run_events (
                run_id TEXT NOT NULL,
                seq INTEGER NOT NULL,
                ts TEXT NOT NULL,
                kind TEXT NOT NULL,
                payload TEXT NOT NULL,
                PRIMARY KEY (run_id, seq)
            )
        """)
        conn.execute("""
            INSERT INTO runs (run_id, thread_id, workspace, status, task, created_at, updated_at)
            VALUES ('run-old-1', 'th-1', '/legacy/ws', 'completed', 'task 1', '2026-08-01T00:00:00Z', '2026-08-01T00:01:00Z')
        """)
        conn.execute("PRAGMA user_version = 1")
        conn.commit()

    # 2. Run additive run-ledger migrations
    applied = run_migrations(db_file, RUNS_MIGRATIONS, baseline_version=1)
    assert applied == [
        "0002_add_runs_principal_and_approvals",
        "0003_add_runs_task_signature",
    ]

    # 3. Verify backup creation
    assert (tmp_path / "runs_test.db.bak-1").exists()

    # 4. Verify version and backfilled data
    with _sqlite_connection(db_file) as conn:
        conn.row_factory = sqlite3.Row
        ver = conn.execute("PRAGMA user_version").fetchone()[0]
        assert ver == 3

        # Verify columns exist
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(runs)").fetchall()}
        assert "workspace_id" in cols
        assert "created_by" in cols
        assert "created_by_kind" in cols
        assert "task_signature" in cols
        assert "workspace" in cols  # Legacy column preserved

        # Verify backfill
        row = conn.execute("SELECT * FROM runs WHERE run_id = 'run-old-1'").fetchone()
        assert row["workspace"] == "/legacy/ws"
        assert row["workspace_id"] == "/legacy/ws"

        # Verify run_approvals table exists
        approval_cols = {
            r["name"] for r in conn.execute("PRAGMA table_info(run_approvals)").fetchall()
        }
        assert {
            "run_id",
            "seq",
            "decision",
            "actor_id",
            "actor_kind",
            "on_behalf_of",
            "reason",
            "decided_at",
        }.issubset(approval_cols)

    # 5. Verify idempotency
    applied_again = run_migrations(db_file, RUNS_MIGRATIONS, baseline_version=1)
    assert applied_again == []


def test_sessions_migration_v1_to_v2(tmp_path: Path):
    from agent_os.migrations import SESSIONS_MIGRATIONS

    db_file = tmp_path / "sessions_test.db"

    # Pre-Phase-0 v1 sessions schema
    with _sqlite_connection(db_file) as conn:
        conn.execute("""
            CREATE TABLE sessions (
                thread_id TEXT PRIMARY KEY,
                created_at TEXT,
                last_turn_at TEXT,
                turn_count INTEGER,
                title TEXT
            )
        """)
        conn.execute("""
            INSERT INTO sessions VALUES ('th-1', '2026-08-01T00:00:00Z', '2026-08-01T00:00:00Z', 1, 'Test')
        """)
        conn.execute("PRAGMA user_version = 1")
        conn.commit()

    applied = run_migrations(db_file, SESSIONS_MIGRATIONS, baseline_version=1)
    assert applied == ["0002_add_sessions_workspace_and_principal"]
    assert (tmp_path / "sessions_test.db.bak-1").exists()

    with _sqlite_connection(db_file) as conn:
        ver = conn.execute("PRAGMA user_version").fetchone()[0]
        assert ver == 2
        cols = {r[1] for r in conn.execute("PRAGMA table_info(sessions)").fetchall()}
        assert "workspace_id" in cols
        assert "created_by" in cols

    # Idempotent
    assert run_migrations(db_file, SESSIONS_MIGRATIONS, baseline_version=1) == []


def test_permissions_migration_v1_to_v2(tmp_path: Path):
    from agent_os.migrations import PERMISSIONS_MIGRATIONS

    db_file = tmp_path / "permissions_test.db"

    # Pre-Phase-0 v1 permissions schema
    with _sqlite_connection(db_file) as conn:
        conn.execute("""
            CREATE TABLE permission_rules (
              permission_key   TEXT PRIMARY KEY,
              effect           TEXT NOT NULL,
              tier_at_creation TEXT NOT NULL,
              approve_count    INTEGER NOT NULL DEFAULT 0,
              deny_count       INTEGER NOT NULL DEFAULT 0,
              created_at       INTEGER NOT NULL,
              updated_at       INTEGER NOT NULL
            )
        """)
        conn.execute("""
            INSERT INTO permission_rules VALUES ('perm:1', 'always_approve', 'low', 1, 0, 1000, 1000)
        """)
        conn.execute("PRAGMA user_version = 1")
        conn.commit()

    applied = run_migrations(db_file, PERMISSIONS_MIGRATIONS, baseline_version=1)
    assert applied == ["0002_add_permission_rules_workspace_and_taught_by"]
    assert (tmp_path / "permissions_test.db.bak-1").exists()

    with _sqlite_connection(db_file) as conn:
        ver = conn.execute("PRAGMA user_version").fetchone()[0]
        assert ver == 2
        cols = {
            r[1] for r in conn.execute("PRAGMA table_info(permission_rules)").fetchall()
        }
        assert "workspace_id" in cols
        assert "taught_by" in cols

    # Idempotent
    assert run_migrations(db_file, PERMISSIONS_MIGRATIONS, baseline_version=1) == []


def test_observations_and_strategy_assignments_workspace_id_invariance(tmp_path: Path):
    from agent_os.observations import SqliteObservationStore

    db_file = tmp_path / "obs_test.db"
    store = SqliteObservationStore(str(db_file))
    assert store is not None

    # Verify observations and strategy_assignments have workspace_id
    with _sqlite_connection(db_file) as conn:
        obs_cols = {r[1] for r in conn.execute("PRAGMA table_info(observations)").fetchall()}
        strat_cols = {
            r[1] for r in conn.execute("PRAGMA table_info(strategy_assignments)").fetchall()
        }
        assert "workspace_id" in obs_cols
        assert "workspace_id" in strat_cols
