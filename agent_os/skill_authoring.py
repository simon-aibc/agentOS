"""Human-gated, validation-first registration of authored skill packages.

This module never drafts a package.  It records a reviewable request, validates
only a human-authored staging package, and registers it only after a second,
explicit confirmation.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import tomllib
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from pydantic import BaseModel, ConfigDict, Field

from agent_os.skill_packages import SkillPackageLoader
from agent_os.skills import SkillRegistry
from agent_os.validation import ValidationRule, run_validation, summarize
from agent_os.workspace import (
    Workspace,
    compose_workspace,
    load_workspace,
)

AUTHORING_DB_NAME: Final = "skill_authoring.db"
DRAFTS_DIRECTORY: Final = "skill-drafts"
APPROVED_DIRECTORY: Final = "skill-packages"
_STATUSES: Final = frozenset(
    {"brief_ready", "validation_failed", "validated", "cancelled", "registered"}
)


class SkillAuthoringError(ValueError):
    """Public, fail-closed errors for the skill authoring gate."""


class CandidateAuthoringInput(BaseModel):
    """Privacy-bounded candidate data supplied by an application candidate ledger."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    candidate_id: str = Field(min_length=1, max_length=160)
    signature: str = Field(min_length=1, max_length=128)
    task_kind: str = Field(min_length=1, max_length=80)
    occurrence_count: int = Field(ge=1)
    success_count: int = Field(ge=0)
    outcome_score: float = Field(ge=0, le=1)
    sample_run_ids: tuple[str, ...] = Field(default_factory=tuple, max_length=3)
    suggested_skill_name: str = Field(min_length=1, max_length=120)
    rationale: str = Field(min_length=1, max_length=900)


class AuthoringRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: str
    workspace_id: str
    candidate: CandidateAuthoringInput
    brief: dict[str, object]
    status: str
    prepared_at: int
    draft_path: str | None = None
    draft_digest: str | None = None
    validation: dict[str, object] | None = None
    registration_confirmed_at: int | None = None
    registered_at: int | None = None
    registration_result: dict[str, object] | None = None
    error: str | None = None


@dataclass(frozen=True)
class ValidationAudit:
    digest: str
    summary: dict[str, object]
    passed: bool
    package_path: str


def authoring_workspace_id(workspace: Workspace) -> str:
    return str(workspace.base_path.resolve())


def authoring_db_path(workspace: Workspace) -> Path:
    return workspace.base_path / AUTHORING_DB_NAME


class SkillAuthoringStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def _init(self) -> None:
        with contextlib.closing(self._connect()) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS skill_authoring_requests (
                    request_id TEXT PRIMARY KEY,
                    workspace_id TEXT NOT NULL,
                    candidate_id TEXT NOT NULL,
                    candidate_json TEXT NOT NULL,
                    brief_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    prepared_at INTEGER NOT NULL,
                    draft_path TEXT,
                    draft_digest TEXT,
                    validation_json TEXT,
                    registration_confirmed_at INTEGER,
                    registered_at INTEGER,
                    registration_result_json TEXT,
                    error TEXT,
                    UNIQUE(workspace_id, candidate_id)
                )
                """
            )
            connection.commit()

    def get(self, request_id: str) -> AuthoringRequest | None:
        with contextlib.closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM skill_authoring_requests WHERE request_id = ?",
                (request_id,),
            ).fetchone()
        return _row_to_request(row) if row else None

    def list(self, workspace_id: str) -> tuple[AuthoringRequest, ...]:
        with contextlib.closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM skill_authoring_requests WHERE workspace_id = ? ORDER BY prepared_at DESC, request_id ASC",
                (workspace_id,),
            ).fetchall()
        return tuple(_row_to_request(row) for row in rows)

    def create(self, request: AuthoringRequest) -> AuthoringRequest:
        with contextlib.closing(self._connect()) as connection:
            try:
                connection.execute(
                    """
                    INSERT INTO skill_authoring_requests (
                        request_id, workspace_id, candidate_id, candidate_json, brief_json,
                        status, prepared_at, draft_path, draft_digest, validation_json,
                        registration_confirmed_at, registered_at, registration_result_json, error
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, NULL, NULL, NULL, NULL)
                    """,
                    (
                        request.request_id,
                        request.workspace_id,
                        request.candidate.candidate_id,
                        request.candidate.model_dump_json(),
                        json.dumps(request.brief, sort_keys=True),
                        request.status,
                        request.prepared_at,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise SkillAuthoringError(
                    "An immutable authoring brief already exists for this candidate"
                ) from exc
            connection.commit()
        return request

    def update_validation(
        self,
        request_id: str,
        *,
        status: str,
        draft_path: str,
        audit: ValidationAudit,
        error: str | None,
    ) -> AuthoringRequest:
        _require_status(status)
        with contextlib.closing(self._connect()) as connection:
            connection.execute(
                """
                UPDATE skill_authoring_requests
                SET status = ?, draft_path = ?, draft_digest = ?, validation_json = ?, error = ?
                WHERE request_id = ?
                """,
                (
                    status,
                    draft_path,
                    audit.digest,
                    json.dumps(audit.summary, sort_keys=True),
                    error,
                    request_id,
                ),
            )
            connection.commit()
        result = self.get(request_id)
        if result is None:
            raise SkillAuthoringError("Authoring request not found")
        return result

    def mark_registered(
        self,
        request_id: str,
        *,
        confirmed_at: int,
        registered_at: int,
        result: Mapping[str, object],
    ) -> AuthoringRequest:
        with contextlib.closing(self._connect()) as connection:
            connection.execute(
                """
                UPDATE skill_authoring_requests
                SET status = 'registered', registration_confirmed_at = ?, registered_at = ?,
                    registration_result_json = ?, error = NULL
                WHERE request_id = ?
                """,
                (
                    confirmed_at,
                    registered_at,
                    json.dumps(dict(result), sort_keys=True),
                    request_id,
                ),
            )
            connection.commit()
        result_request = self.get(request_id)
        if result_request is None:
            raise SkillAuthoringError("Authoring request not found")
        return result_request


class SkillAuthoringService:
    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace
        self.workspace_id = authoring_workspace_id(workspace)
        self.store = SkillAuthoringStore(authoring_db_path(workspace))

    def prepare_brief(
        self, candidate: CandidateAuthoringInput, *, now: int
    ) -> AuthoringRequest:
        request_id = str(uuid.uuid4())
        brief = _build_brief(request_id, candidate, self.workspace)
        return self.store.create(
            AuthoringRequest(
                request_id=request_id,
                workspace_id=self.workspace_id,
                candidate=candidate,
                brief=brief,
                status="brief_ready",
                prepared_at=now,
            )
        )

    def submit_draft(
        self, request_id: str, draft_path: str, *, now: int
    ) -> AuthoringRequest:
        request = self._require_request(
            request_id, allowed={"brief_ready", "validation_failed", "validated"}
        )
        package_dir = resolve_staging_package(self.workspace, draft_path)
        audit = validate_staged_package(
            package_dir, request.candidate.suggested_skill_name, self.workspace
        )
        return self.store.update_validation(
            request_id,
            status="validated" if audit.passed else "validation_failed",
            draft_path=str(package_dir),
            audit=audit,
            error=None
            if audit.passed
            else "Validation did not meet every deterministic rule",
        )

    def confirm_registration(
        self, request_id: str, *, confirm: bool, now: int
    ) -> AuthoringRequest:
        if confirm is not True:
            raise SkillAuthoringError(
                "Final registration requires explicit confirm=true"
            )
        request = self._require_request(request_id, allowed={"validated"})
        if not request.draft_path or not request.draft_digest:
            raise SkillAuthoringError("Validated draft evidence is missing")
        package_dir = resolve_staging_package(self.workspace, request.draft_path)
        if package_digest(package_dir) != request.draft_digest:
            raise SkillAuthoringError(
                "Draft changed after validation; submit it for validation again"
            )
        audit = validate_staged_package(
            package_dir, request.candidate.suggested_skill_name, self.workspace
        )
        if not audit.passed or audit.digest != request.draft_digest:
            self.store.update_validation(
                request_id,
                status="validation_failed",
                draft_path=str(package_dir),
                audit=audit,
                error="Revalidation failed before registration",
            )
            raise SkillAuthoringError(
                "Revalidation failed; registry and workspace configuration were left unchanged"
            )

        result = promote_and_register(
            self.workspace, package_dir, request.candidate.suggested_skill_name
        )
        return self.store.mark_registered(
            request_id, confirmed_at=now, registered_at=now, result=result
        )

    def list_requests(self) -> tuple[AuthoringRequest, ...]:
        return self.store.list(self.workspace_id)

    def cancel(self, request_id: str) -> AuthoringRequest:
        request = self._require_request(
            request_id, allowed={"brief_ready", "validation_failed", "validated"}
        )
        with contextlib.closing(self.store._connect()) as connection:
            connection.execute(
                "UPDATE skill_authoring_requests SET status = 'cancelled', error = NULL WHERE request_id = ?",
                (request.request_id,),
            )
            connection.commit()
        cancelled = self.store.get(request_id)
        if cancelled is None:
            raise SkillAuthoringError("Authoring request not found")
        return cancelled

    def _require_request(
        self, request_id: str, *, allowed: set[str]
    ) -> AuthoringRequest:
        request = self.store.get(request_id)
        if request is None or request.workspace_id != self.workspace_id:
            raise SkillAuthoringError("Authoring request not found")
        if request.status not in allowed:
            raise SkillAuthoringError(
                f"Authoring request is not ready for this action: {request.status}"
            )
        return request


def _build_brief(
    request_id: str, candidate: CandidateAuthoringInput, workspace: Workspace
) -> dict[str, object]:
    return {
        "version": "skill-authoring-v1",
        "request_id": request_id,
        "candidate_id": candidate.candidate_id,
        "suggested_skill_name": candidate.suggested_skill_name,
        "task_signature": candidate.signature,
        "task_kind": candidate.task_kind,
        "rationale": candidate.rationale,
        "sample_run_ids": list(candidate.sample_run_ids),
        "staging_directory": str((workspace.base_path / DRAFTS_DIRECTORY).resolve()),
        "constraints": [
            "A human must author the package; this request does not generate code.",
            "Keep the draft outside configured skill directories until validation passes.",
            "Registration requires a separate explicit confirmation after deterministic validation.",
        ],
    }


def resolve_staging_package(workspace: Workspace, value: str) -> Path:
    raw = Path(str(value)).expanduser()
    if not raw.is_absolute():
        raise SkillAuthoringError(
            "Draft path must be an absolute canonical staging path"
        )
    try:
        package_dir = raw.resolve(strict=True)
    except OSError as exc:
        raise SkillAuthoringError("Draft package path does not exist") from exc
    staging_root = (workspace.base_path / DRAFTS_DIRECTORY).resolve()
    if not _is_relative_to(package_dir, staging_root) or package_dir == staging_root:
        raise SkillAuthoringError(
            "Draft package must be inside the canonical skill-drafts directory"
        )
    if not package_dir.is_dir() or not (package_dir / "manifest.toml").is_file():
        raise SkillAuthoringError("Draft package requires a manifest.toml directory")
    for configured in workspace.skills:
        configured_path = Path(configured).expanduser().resolve()
        if _is_relative_to(package_dir, configured_path) or _is_relative_to(
            configured_path, package_dir
        ):
            raise SkillAuthoringError(
                "Draft package must remain outside configured skill directories"
            )
    return package_dir


def package_digest(package_dir: Path) -> str:
    digest = hashlib.sha256()
    for entry in sorted(package_dir.rglob("*")):
        if entry.is_symlink():
            raise SkillAuthoringError("Draft packages cannot contain symbolic links")
        if not entry.is_file():
            continue
        # Python's isolated import preflight can generate these artifacts.
        # They are not human-authored package content and must not turn a
        # passing draft into a false TOCTOU mismatch.
        if "__pycache__" in entry.parts or entry.suffix == ".pyc":
            continue
        relative = entry.relative_to(package_dir).as_posix().encode("utf-8")
        digest.update(relative)
        digest.update(b"\0")
        digest.update(entry.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def validate_staged_package(
    package_dir: Path, expected_name: str, workspace: Workspace
) -> ValidationAudit:
    digest = package_digest(package_dir)
    command = f"skill-package-preflight:{package_dir}"
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "agent_os.skill_authoring_preflight",
            str(package_dir),
            expected_name,
        ],
        cwd=workspace.base_path,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    output = "\n".join(
        part for part in (completed.stdout, completed.stderr) if part
    ).strip()
    rules = (
        ValidationRule(
            id="manifest-present",
            description="Draft package includes manifest.toml",
            kind="deterministic",
            check="file_exists",
            param=str(package_dir / "manifest.toml"),
        ),
        ValidationRule(
            id="isolated-loader-preflight",
            description="Fresh registry loads the manifest and expected handler",
            kind="deterministic",
            check="verify_cmd",
            param=command,
        ),
    )
    summary = summarize(
        run_validation(
            rules,
            {
                "artifacts": [str(package_dir / "manifest.toml")],
                "verify_commands": {
                    command: {"exit_code": completed.returncode, "output": output}
                },
            },
        )
    )
    return ValidationAudit(
        digest=digest,
        summary=summary,
        passed=summary["total"] > 0 and summary["met"] == summary["total"],
        package_path=str(package_dir),
    )


def promote_and_register(
    workspace: Workspace, package_dir: Path, expected_name: str
) -> dict[str, object]:
    """Copy an already-validated package and atomically add its exact path to the workspace."""
    destination_root = (workspace.base_path / APPROVED_DIRECTORY).resolve()
    destination_root.mkdir(parents=True, exist_ok=True)
    destination = (destination_root / expected_name).resolve()
    if not _is_relative_to(destination, destination_root) or destination.exists():
        raise SkillAuthoringError("Approved package destination is unavailable")
    temp_destination = destination_root / f".{expected_name}.{uuid.uuid4().hex}.tmp"
    shutil.copytree(package_dir, temp_destination, symlinks=False)
    moved_destination = False
    try:
        _assert_fresh_loader(temp_destination, expected_name)
        source_config = workspace.base_path / "workspace.toml"
        original = source_config.read_text(encoding="utf-8")
        updated = add_skill_path_to_workspace_toml(original, str(destination))
        config_temp = _write_temp_config(source_config, updated)
        try:
            os.replace(temp_destination, destination)
            moved_destination = True
            _assert_workspace_config_loads(config_temp)
            os.replace(config_temp, source_config)
            _reload_workspace_or_rollback(source_config, original)
        except Exception:
            with contextlib.suppress(FileNotFoundError):
                config_temp.unlink()
            if moved_destination:
                with contextlib.suppress(FileNotFoundError):
                    shutil.rmtree(destination)
            raise
    except Exception:
        with contextlib.suppress(FileNotFoundError):
            shutil.rmtree(temp_destination)
        raise
    return {"package_path": str(destination), "registered_skill_name": expected_name}


def _assert_fresh_loader(package_dir: Path, expected_name: str) -> None:
    registry = SkillRegistry()
    SkillPackageLoader(registry).load_package(package_dir)
    if registry.get(expected_name) is None:
        raise SkillAuthoringError(
            "Draft did not register the expected skill name in an isolated registry"
        )


def _write_temp_config(source: Path, content: str) -> Path:
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=source.parent,
        prefix=".workspace.",
        suffix=".toml",
        delete=False,
    ) as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
        return Path(handle.name)


def _assert_workspace_config_loads(config_path: Path) -> None:
    # Validate package/config integration in a fresh registry before persisting it.
    load_workspace(config_path)


def _reload_workspace_or_rollback(source_config: Path, original: str) -> None:
    from agent_os.server import runtime

    runtime.composed_workspace.cache_clear()
    try:
        # Compose the precise config that was atomically written first.  The
        # server cache is then reloaded only after that isolated proof passes.
        compose_workspace(load_workspace(source_config))
        runtime.composed_workspace()
    except Exception as exc:
        rollback = _write_temp_config(source_config, original)
        os.replace(rollback, source_config)
        runtime.composed_workspace.cache_clear()
        raise SkillAuthoringError(
            "Workspace reload failed; configuration was rolled back"
        ) from exc


def add_skill_path_to_workspace_toml(original: str, path: str) -> str:
    """Replace exactly the top-level skills list while preserving all other TOML text."""
    parsed = tomllib.loads(original)
    skills = parsed.get("skills")
    if not isinstance(skills, list) or not all(
        isinstance(item, str) for item in skills
    ):
        raise SkillAuthoringError(
            "workspace.toml has no writable top-level skills list"
        )
    if path in skills:
        raise SkillAuthoringError("Skill path is already configured")
    start = _top_level_skills_list_start(original)
    end = _matching_list_end(original, start)
    rendered = (
        "skills = [\n"
        + "".join(f"  {json.dumps(item)},\n" for item in [*skills, path])
        + "]"
    )
    updated = original[:start] + rendered + original[end:]
    try:
        tomllib.loads(updated)
    except tomllib.TOMLDecodeError as exc:
        raise SkillAuthoringError(
            "Could not write a valid workspace skills list"
        ) from exc
    return updated


def _top_level_skills_list_start(content: str) -> int:
    marker = "skills"
    for line_start, line in _lines_with_offsets(content):
        stripped = line.lstrip()
        if stripped.startswith(marker) and stripped[len(marker) :].lstrip().startswith(
            "="
        ):
            return line_start
    raise SkillAuthoringError("workspace.toml has no writable top-level skills list")


def _matching_list_end(content: str, start: int) -> int:
    opening = content.find("[", start)
    if opening < 0:
        raise SkillAuthoringError("workspace.toml skills list is malformed")
    depth = 0
    quote: str | None = None
    escaped = False
    for index in range(opening, len(content)):
        char = content[index]
        if quote is not None:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in {"'", '"'}:
            quote = char
        elif char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
            if depth == 0:
                return index + 1
    raise SkillAuthoringError("workspace.toml skills list is malformed")


def _lines_with_offsets(value: str):
    offset = 0
    for line in value.splitlines(keepends=True):
        yield offset, line
        offset += len(line)


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _require_status(status: str) -> None:
    if status not in _STATUSES:
        raise SkillAuthoringError(f"Unknown authoring request status: {status}")


def _row_to_request(row: sqlite3.Row) -> AuthoringRequest:
    return AuthoringRequest(
        request_id=row["request_id"],
        workspace_id=row["workspace_id"],
        candidate=CandidateAuthoringInput.model_validate_json(row["candidate_json"]),
        brief=json.loads(row["brief_json"]),
        status=row["status"],
        prepared_at=int(row["prepared_at"]),
        draft_path=row["draft_path"],
        draft_digest=row["draft_digest"],
        validation=json.loads(row["validation_json"])
        if row["validation_json"]
        else None,
        registration_confirmed_at=row["registration_confirmed_at"],
        registered_at=row["registered_at"],
        registration_result=json.loads(row["registration_result_json"])
        if row["registration_result_json"]
        else None,
        error=row["error"],
    )
