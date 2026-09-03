"""Tests asserting scheduler SQLite isolation (r1.8a.1).

Proves:
- Derived path includes ``.sched`` infix.
- ``AGENT_OS_SCHED_DB`` override works.
- WAL and busy_timeout are set.
- Scheduler tables never appear in the checkpoint DB.
- The scheduler file is distinct while an async-style connection is open.
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
from pathlib import Path

from agent_os.checkpoints import CHECKPOINT_DB_ENV, DEFAULT_CHECKPOINT_DB
from agent_os.schedules import (
    SCHED_DB_ENV,
    _connect,
    _get_db_path,
    _init_schedules_db,
)

# ── Path derivation ─────────────────────────────────────────────────


def test_derived_path_inserts_sched_before_extension(monkeypatch):
    monkeypatch.setenv(CHECKPOINT_DB_ENV, "/data/state.sqlite")
    monkeypatch.delenv(SCHED_DB_ENV, raising=False)
    assert _get_db_path() == "/data/state.sched.sqlite"


def test_derived_path_extensionless(monkeypatch):
    monkeypatch.setenv(CHECKPOINT_DB_ENV, "/data/checkpoints")
    monkeypatch.delenv(SCHED_DB_ENV, raising=False)
    assert _get_db_path() == "/data/checkpoints.sched.db"


def test_derived_path_default(monkeypatch):
    monkeypatch.delenv(CHECKPOINT_DB_ENV, raising=False)
    monkeypatch.delenv(SCHED_DB_ENV, raising=False)
    root, ext = os.path.splitext(DEFAULT_CHECKPOINT_DB)
    assert _get_db_path() == f"{root}.sched{ext or '.db'}"


def test_override_env(monkeypatch, tmp_path):
    override = str(tmp_path / "my-scheduler.db")
    monkeypatch.setenv(SCHED_DB_ENV, override)
    assert _get_db_path() == override


def test_memory_path(monkeypatch):
    monkeypatch.setenv(CHECKPOINT_DB_ENV, ":memory:")
    monkeypatch.delenv(SCHED_DB_ENV, raising=False)
    assert _get_db_path() == ":memory:"


# ── WAL and busy timeout ────────────────────────────────────────────


def test_connection_uses_wal_and_busy_timeout(tmp_path, monkeypatch):
    monkeypatch.setenv(SCHED_DB_ENV, str(tmp_path / "sched.db"))
    with contextlib.closing(_connect()) as conn:
        journal = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert journal == "wal"
        timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
        assert timeout == 30000


# ── No scheduler tables in checkpoint DB ────────────────────────────


def test_no_schedules_table_in_checkpoint_db(tmp_path, monkeypatch):
    ckpt = str(tmp_path / "checkpoints.db")
    monkeypatch.setenv(CHECKPOINT_DB_ENV, ckpt)
    monkeypatch.delenv(SCHED_DB_ENV, raising=False)

    # Create the checkpoint DB manually.
    with contextlib.closing(sqlite3.connect(ckpt)) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS dummy (id TEXT)")
        conn.commit()

    # Init the scheduler.
    _init_schedules_db()

    # Verify the checkpoint DB has no scheduler tables.
    with contextlib.closing(sqlite3.connect(ckpt)) as conn:
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'schedules'"
        )
        assert cursor.fetchone() is None, (
            "Scheduler table was created in the checkpoint DB"
        )


# ── Separate file while an async-style connection is open ───────────


def test_separate_file_while_checkpoint_connection_open(tmp_path, monkeypatch):
    ckpt = str(tmp_path / "checkpoints.db")
    monkeypatch.setenv(CHECKPOINT_DB_ENV, ckpt)
    monkeypatch.delenv(SCHED_DB_ENV, raising=False)

    sched_path = _get_db_path()
    assert sched_path != ckpt

    # Simulate an async checkpointer holding a connection.
    ckpt_conn = sqlite3.connect(ckpt, check_same_thread=False)
    ckpt_conn.execute("CREATE TABLE IF NOT EXISTS dummy (id TEXT)")
    ckpt_conn.commit()

    try:
        _init_schedules_db()
        assert Path(sched_path).exists()
        assert Path(ckpt).exists()

        # Verify both files are distinct filesystem paths.
        assert Path(sched_path).resolve() != Path(ckpt).resolve()
    finally:
        ckpt_conn.close()


# ── File creation ───────────────────────────────────────────────────


def test_init_creates_db_file(tmp_path, monkeypatch):
    db_path = str(tmp_path / "sched.db")
    monkeypatch.setenv(SCHED_DB_ENV, db_path)
    _init_schedules_db()
    assert Path(db_path).exists()
