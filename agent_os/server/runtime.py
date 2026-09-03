from __future__ import annotations

import os
from functools import lru_cache
from importlib.metadata import PackageNotFoundError, version
from typing import Any

from langchain_core.messages import HumanMessage

from agent_os.bindings import resolve_backend_binding
from agent_os.connectors import MemoryConnector
from agent_os.nodes.tool_dispatcher import build_tool_dispatcher_node
from agent_os.observations import (
    observation_workspace_id,
    open_observation_store,
)
from agent_os.policy import LocalPolicy
from agent_os.routing import build_runtime_config
from agent_os.strategies import select_strategy
from agent_os.workspace import (
    ComposedWorkspace,
    compose_workspace,
    load_workspace,
    open_permission_store,
)

PACKAGE_NAME = "agent-os-langgraph"


def package_version() -> str:
    try:
        return version(PACKAGE_NAME)
    except PackageNotFoundError:
        return "2.0.0"


@lru_cache(maxsize=1)
def composed_workspace() -> ComposedWorkspace | None:
    workspace_path = os.getenv("AGENT_OS_WORKSPACE", "").strip()
    if not workspace_path:
        return None
    return compose_workspace(load_workspace(workspace_path))


def runtime_config(thread_id: str) -> dict[str, object]:
    config = build_runtime_config(thread_id)
    runtime = composed_workspace()
    if runtime is None:
        return config

    configurable = config.setdefault("configurable", {})
    if isinstance(configurable, dict):
        configurable["workspace"] = runtime

    recursion_limit = runtime.limits.get("recursion_limit")
    if isinstance(recursion_limit, int) and recursion_limit > 0:
        config["recursion_limit"] = recursion_limit
    return config


def runtime_policy_for_session(session_key: str | None) -> LocalPolicy:
    """Return the active workspace policy bound to one run/session."""
    runtime = composed_workspace()
    if runtime is not None:
        return runtime.policy.with_session(session_key)
    return LocalPolicy(store=open_permission_store(), session_key=session_key)


def initial_state(
    task: str,
    *,
    run_id: str | None = None,
    strategy_override: str | None = None,
) -> dict[str, object]:
    runtime = composed_workspace()
    backend_binding = (
        runtime.backend_binding
        if runtime is not None
        else resolve_backend_binding(None)
    )
    hot_context = runtime.hot_context if runtime is not None else None
    workspace = runtime.workspace if runtime is not None else None
    task_kind = task_kind_for_input(task)
    selection = select_strategy(
        # WebSocket/chat inputs do not yet have a durable run id, so they use
        # the fixed fallback rather than opening a store they cannot attribute.
        open_observation_store(workspace) if run_id is not None else None,
        workspace_id=observation_workspace_id(workspace),
        task_kind=task_kind,
        run_id=run_id,
        explicit_strategy_id=strategy_override,
    )
    return {
        "messages": [HumanMessage(content=task)],
        "task": task,
        "run_id": run_id,
        "plan": None,
        "executor_output": None,
        "human_feedback": None,
        "hot_context": hot_context,
        # Raw outcome evidence stays review-only; the architect gets only a
        # fixed, structured directive selected from aggregated labels.
        "observation_context": None,
        "strategy_hint": selection.hint,
        "conversation_summary": None,
        "backend_binding": backend_binding,
    }


def task_kind_for_input(task: str) -> str:
    """Classify only a known native command; never persist the task itself."""
    return (
        "memory_write"
        if task.lstrip().lower().startswith("memory_write")
        else "workflow"
    )


def build_runtime_graph(*, checkpointer: Any) -> Any:
    from agent_os.graph import build_graph

    runtime = composed_workspace()
    if runtime is None:
        return build_graph(checkpointer=checkpointer)
    return build_graph(
        tool_dispatcher_node_impl=build_tool_dispatcher_node(runtime.skill_registry),
        checkpointer=checkpointer,
    )


def memory_connector() -> MemoryConnector:
    runtime = composed_workspace()
    if runtime is not None:
        return runtime.memory_connector
    from agent_os.tools.memory_write import default_memory_connector

    return default_memory_connector()


def runtime_summary() -> dict[str, object]:
    workspace_path = os.getenv("AGENT_OS_WORKSPACE", "").strip()
    try:
        runtime = composed_workspace()
    except Exception as error:
        return {
            "configured": bool(workspace_path),
            "status": "error",
            "path": workspace_path or None,
            "error": str(error),
        }
    if runtime is None:
        return {"configured": False, "status": "not_configured"}
    return {
        "configured": True,
        "status": "ok",
        "path": str(runtime.workspace.base_path / "workspace.toml"),
        "name": runtime.workspace.workspace.name,
        "memory_connector": runtime.memory_connector.name,
        "skills": len(runtime.skill_registry.names()),
        "hot_context": bool(runtime.hot_context),
        "recursion_limit": runtime.limits.get("recursion_limit"),
    }
