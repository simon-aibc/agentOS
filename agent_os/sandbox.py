import os
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

_sandbox_override: ContextVar[Path | None] = ContextVar(
    "agent_os_sandbox_override",
    default=None,
)


def _configured_sandbox_root() -> Path:
    sandbox_env = os.getenv("AGENT_OS_SANDBOX", "./sandbox")
    return Path(sandbox_env).resolve()


def get_sandbox_root() -> Path:
    """
    Read AGENT_OS_SANDBOX on every call.
    Default is './sandbox'.
    Relative values resolve from Path.cwd().
    Returns a resolved absolute Path.
    """
    override = _sandbox_override.get()
    if override is not None:
        return override
    return _configured_sandbox_root()


def get_read_root() -> Path:
    """Use an explicit sandbox for reads, otherwise preserve cwd behavior."""
    override = _sandbox_override.get()
    if override is not None:
        return override
    configured_root = os.getenv("AGENT_OS_SANDBOX")
    if configured_root:
        return Path(configured_root).resolve()
    return Path.cwd().resolve()


def resolve_workspace_root(workspace: str | None) -> Path:
    """Resolve a run workspace and confine it to ``AGENT_OS_SANDBOX``.

    A missing workspace selects the configured sandbox itself. Relative paths
    are resolved beneath that root. The target must already be a directory so
    a typo cannot silently create or execute in an unintended location.
    """
    base = _configured_sandbox_root()
    requested = Path(workspace) if workspace and workspace.strip() else base
    if not requested.is_absolute():
        requested = base / requested
    resolved = requested.resolve()

    if not resolved.is_relative_to(base):
        raise ValueError(f"Workspace {workspace!r} resolves outside the sandbox")
    if not resolved.is_dir():
        raise ValueError(f"Workspace {workspace!r} is not an existing directory")
    return resolved


@contextmanager
def sandbox_scope(workspace: str | None) -> Iterator[Path]:
    """Temporarily bind all sandbox consumers to one validated workspace."""
    resolved = resolve_workspace_root(workspace)
    token = _sandbox_override.set(resolved)
    try:
        yield resolved
    finally:
        _sandbox_override.reset(token)


def resolve_sandbox_path(path: str) -> Path:
    """
    Resolves a user path and rejects anything outside the sandbox,
    including traversal and symlink escapes.
    Does not create directories or mutate the filesystem.
    """
    sandbox_root = get_sandbox_root()
    target_path = Path(path)

    if not target_path.is_absolute():
        target_path = sandbox_root / target_path

    resolved_path = target_path.resolve()

    if not resolved_path.is_relative_to(sandbox_root):
        raise ValueError(f"Path {path} resolves outside the sandbox")

    return resolved_path
