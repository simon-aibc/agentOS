import os
from collections.abc import Callable
from pathlib import Path

from agent_os.backends import get_default_backend_registry
from agent_os.sandbox import get_sandbox_root
from agent_os.schemas import PlanArtifact
from agent_os.state import AgentState

MAX_INVENTORY_FILES = 1000
_MAX_INVENTORY_DIRECTORIES = 1000
_IGNORED_INVENTORY_DIRECTORIES = frozenset(
    {
        ".cache",
        ".codegraph",
        ".git",
        ".graphify",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "__pycache__",
        "build",
        "dist",
        "node_modules",
        "venv",
    }
)


def _build_sandbox_inventory() -> list[str]:
    """
    Build a stable, sorted list of files under the sandbox root.
    Returns relative paths as strings.
    Ignores symlinks pointing outside the sandbox.
    Bounds the listing to MAX_INVENTORY_FILES to prevent unbounded prompts.
    """
    sandbox = get_sandbox_root()
    if not sandbox.exists():
        return []

    valid_files: list[str] = []
    visited_directories = 0

    for root, dirs, files in os.walk(sandbox, followlinks=False):
        if len(valid_files) >= MAX_INVENTORY_FILES:
            break
        visited_directories += 1
        if visited_directories > _MAX_INVENTORY_DIRECTORIES:
            break

        root_path = Path(root)
        safe_dirs: list[str] = []
        for directory in sorted(dirs):
            if directory in _IGNORED_INVENTORY_DIRECTORIES:
                continue
            directory_path = root_path / directory
            try:
                if directory_path.is_symlink():
                    continue
                if directory_path.resolve().is_relative_to(sandbox):
                    safe_dirs.append(directory)
            except (OSError, RuntimeError):
                continue
        dirs[:] = safe_dirs

        for filename in sorted(files):
            if len(valid_files) >= MAX_INVENTORY_FILES:
                break

            file_path = root_path / filename
            try:
                resolved_file = file_path.resolve()
                if resolved_file.is_relative_to(sandbox) and resolved_file.is_file():
                    rel_path = file_path.relative_to(sandbox)
                    valid_files.append(rel_path.as_posix())
            except (OSError, RuntimeError, ValueError):
                continue

    return sorted(valid_files)


def _build_architect_prompt(state: AgentState) -> str:
    """
    Build the prompt text for the CLI architect.
    Includes the original task, sandbox inventory, and any rejection feedback.
    """
    inventory = _build_sandbox_inventory()

    prompt = f"Task:\n{state['task']}\n\n"

    strategy_hint = state.get("strategy_hint")
    if strategy_hint:
        strategy_data = (
            strategy_hint.model_dump()
            if hasattr(strategy_hint, "model_dump")
            else strategy_hint
        )
        if not isinstance(strategy_data, dict):
            strategy_data = {}
        prompt += (
            "Planning strategy:\n"
            f"Selected strategy: {strategy_data.get('strategy_id', 'default-v1')}\n"
            f"Directive: {strategy_data.get('directive', '')}\n\n"
        )

    if inventory:
        prompt += "Available files in sandbox:\n"
        for f in inventory:
            prompt += f"- {f}\n"
        prompt += "\n"
    else:
        prompt += "Available files in sandbox: (empty)\n\n"

    feedback = state.get("human_feedback")
    if feedback and feedback.startswith("rejected:"):
        prompt += f"Previous plan was rejected with feedback:\n{feedback}\n\n"

    prompt += "Please analyze the task and return a structured plan matching the CodingPlan schema. Do not execute any code directly."

    return prompt


def build_cli_architect_invoker(
    backend: str,
) -> Callable[[AgentState], PlanArtifact]:
    """
    Returns a callable that executes the CLI architect node logic for a specific backend.
    Supported backends: 'claude-code', 'codex'.
    """

    try:
        adapter = get_default_backend_registry().resolve("architect", backend)
    except ValueError as exc:
        raise ValueError(
            f"Unsupported CLI architect backend: {backend}. {exc}"
        ) from exc
    return adapter.build_invoker("architect")
