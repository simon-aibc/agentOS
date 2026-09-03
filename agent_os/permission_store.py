import re
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol, runtime_checkable

from agent_os.migrations import PERMISSIONS_MIGRATIONS, run_migrations
from agent_os.stores.base import configure_sqlite_connection

PERMISSION_DB_ENV = "AGENT_OS_PERMISSIONS_DB"
DEFAULT_PERMISSION_DB = "./permissions.db"

# Keep legacy three-part keys manageable/revocable while accepting the five
# parts used by safely scoped memory rules:
# ``memory_write:write:<connector>:<mode>:<ref>``.
PERMISSION_KEY_REGEX = re.compile(
    r"^[a-z0-9_]+:[a-z0-9_]+:(?:[a-z0-9_]+:[a-z0-9_]+:)?[^\s:]+$",
    re.IGNORECASE,
)


@dataclass
class PermissionRule:
    """A learned user-taught permission rule."""

    permission_key: str
    effect: Literal["always_approve", "always_deny"]
    tier_at_creation: Literal["trivial", "low", "medium", "high"]
    approve_count: int = 0
    deny_count: int = 0
    created_at: int = 0  # epoch MILLIseconds
    updated_at: int = 0
    workspace_id: str | None = None
    taught_by: str | None = None


@runtime_checkable
class PermissionRuleStore(Protocol):
    """Protocol for storing learned permission rules."""

    def get(self, permission_key: str) -> PermissionRule | None: ...

    def upsert(self, rule: PermissionRule) -> None: ...

    def list(self) -> list[PermissionRule]: ...

    def delete(self, permission_key: str) -> bool: ...

    def increment_approve(self, permission_key: str) -> None: ...

    def increment_deny(self, permission_key: str) -> None: ...


PermissionStore = PermissionRuleStore


class SqlitePermissionStore:
    """SQLite-backed permission rule store with WAL and busy timeout safety."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._init_db()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        path = Path(self.db_path).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, timeout=10.0)
        try:
            configure_sqlite_connection(conn, wal=True, busy_timeout_ms=5000)
            yield conn
        finally:
            conn.close()

    def _init_db(self) -> None:
        path = str(Path(self.db_path).expanduser())
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS permission_rules (
                  permission_key   TEXT PRIMARY KEY,
                  effect           TEXT NOT NULL,
                  tier_at_creation TEXT NOT NULL,
                  approve_count    INTEGER NOT NULL DEFAULT 0,
                  deny_count       INTEGER NOT NULL DEFAULT 0,
                  created_at       INTEGER NOT NULL,
                  updated_at       INTEGER NOT NULL
                );
            """)
            conn.commit()
        run_migrations(path, PERMISSIONS_MIGRATIONS, baseline_version=1)

    def _validate_key(self, permission_key: str) -> None:
        if not isinstance(permission_key, str) or not PERMISSION_KEY_REGEX.fullmatch(
            permission_key
        ):
            raise ValueError(f"Invalid permission_key format: {permission_key}")

    def get(self, permission_key: str) -> PermissionRule | None:
        self._validate_key(permission_key)
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT permission_key, effect, tier_at_creation, approve_count, deny_count,
                       created_at, updated_at, workspace_id, taught_by
                FROM permission_rules
                WHERE permission_key = ?
                """,
                (permission_key,),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            return PermissionRule(
                permission_key=row[0],
                effect=row[1],
                tier_at_creation=row[2],
                approve_count=row[3],
                deny_count=row[4],
                created_at=row[5],
                updated_at=row[6],
                workspace_id=row[7],
                taught_by=row[8],
            )

    def upsert(self, rule: PermissionRule) -> None:
        self._validate_key(rule.permission_key)
        now_ms = int(time.time() * 1000)
        created_at = rule.created_at if rule.created_at else now_ms
        updated_at = now_ms

        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO permission_rules (
                  permission_key, effect, tier_at_creation, approve_count, deny_count,
                  created_at, updated_at, workspace_id, taught_by
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(permission_key) DO UPDATE SET
                  effect = excluded.effect,
                  tier_at_creation = excluded.tier_at_creation,
                  updated_at = excluded.updated_at,
                  workspace_id = COALESCE(excluded.workspace_id, permission_rules.workspace_id),
                  taught_by = COALESCE(excluded.taught_by, permission_rules.taught_by)
                """,
                (
                    rule.permission_key,
                    rule.effect,
                    rule.tier_at_creation,
                    rule.approve_count,
                    rule.deny_count,
                    created_at,
                    updated_at,
                    rule.workspace_id,
                    rule.taught_by,
                ),
            )
            conn.commit()

    def list(self) -> list[PermissionRule]:
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT permission_key, effect, tier_at_creation, approve_count, deny_count,
                       created_at, updated_at, workspace_id, taught_by
                FROM permission_rules
                ORDER BY permission_key ASC
                """
            )
            rows = cursor.fetchall()
            return [
                PermissionRule(
                    permission_key=r[0],
                    effect=r[1],
                    tier_at_creation=r[2],
                    approve_count=r[3],
                    deny_count=r[4],
                    created_at=r[5],
                    updated_at=r[6],
                    workspace_id=r[7],
                    taught_by=r[8],
                )
                for r in rows
            ]

    def delete(self, permission_key: str) -> bool:
        self._validate_key(permission_key)
        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM permission_rules WHERE permission_key = ?",
                (permission_key,),
            )
            conn.commit()
            return cursor.rowcount > 0

    def increment_approve(self, permission_key: str) -> None:
        self._validate_key(permission_key)
        now_ms = int(time.time() * 1000)
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE permission_rules
                SET approve_count = approve_count + 1, updated_at = ?
                WHERE permission_key = ?
                """,
                (now_ms, permission_key),
            )
            conn.commit()

    def increment_deny(self, permission_key: str) -> None:
        self._validate_key(permission_key)
        now_ms = int(time.time() * 1000)
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE permission_rules
                SET deny_count = deny_count + 1, updated_at = ?
                WHERE permission_key = ?
                """,
                (now_ms, permission_key),
            )
            conn.commit()


class InMemoryPermissionStore:
    """In-memory permission rule store for testing."""

    def __init__(self) -> None:
        self._rules: dict[str, PermissionRule] = {}

    def _validate_key(self, permission_key: str) -> None:
        if not isinstance(permission_key, str) or not PERMISSION_KEY_REGEX.fullmatch(
            permission_key
        ):
            raise ValueError(f"Invalid permission_key format: {permission_key}")

    def get(self, permission_key: str) -> PermissionRule | None:
        self._validate_key(permission_key)
        rule = self._rules.get(permission_key)
        if rule is None:
            return None
        return PermissionRule(**rule.__dict__)

    def upsert(self, rule: PermissionRule) -> None:
        self._validate_key(rule.permission_key)
        now_ms = int(time.time() * 1000)
        existing = self._rules.get(rule.permission_key)
        if existing:
            existing.effect = rule.effect
            existing.tier_at_creation = rule.tier_at_creation
            existing.updated_at = now_ms
            if rule.workspace_id is not None:
                existing.workspace_id = rule.workspace_id
            if rule.taught_by is not None:
                existing.taught_by = rule.taught_by
        else:
            created_at = rule.created_at if rule.created_at else now_ms
            new_rule = PermissionRule(
                permission_key=rule.permission_key,
                effect=rule.effect,
                tier_at_creation=rule.tier_at_creation,
                approve_count=rule.approve_count,
                deny_count=rule.deny_count,
                created_at=created_at,
                updated_at=now_ms,
                workspace_id=rule.workspace_id,
                taught_by=rule.taught_by,
            )
            self._rules[rule.permission_key] = new_rule

    def list(self) -> list[PermissionRule]:
        return [
            PermissionRule(**r.__dict__)
            for r in sorted(self._rules.values(), key=lambda x: x.permission_key)
        ]

    def delete(self, permission_key: str) -> bool:
        self._validate_key(permission_key)
        return self._rules.pop(permission_key, None) is not None

    def increment_approve(self, permission_key: str) -> None:
        self._validate_key(permission_key)
        rule = self._rules.get(permission_key)
        if rule:
            rule.approve_count += 1
            rule.updated_at = int(time.time() * 1000)

    def increment_deny(self, permission_key: str) -> None:
        self._validate_key(permission_key)
        rule = self._rules.get(permission_key)
        if rule:
            rule.deny_count += 1
            rule.updated_at = int(time.time() * 1000)
