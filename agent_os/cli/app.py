import argparse
import asyncio
import datetime as dt
import ipaddress
import os
import sys
import traceback
import uuid
from collections.abc import Callable
from typing import Any

from rich.console import Console

from agent_os.cli.formatter import EventFormatter
from agent_os.cli.parser import format_event
from agent_os.state import BackendBinding

GraphFactory = Callable[[], Any]
InputFunction = Callable[[str], str]


def build_parser() -> argparse.ArgumentParser:
    """Build the public command-line contract."""
    parser = argparse.ArgumentParser(
        prog="agent-os",
        description="Run the Agent OS LangGraph workflow.",
    )
    parser.add_argument(
        "--workspace", help="Path to workspace.toml or a workspace directory."
    )
    subparsers = parser.add_subparsers(dest="command")

    run_parser = subparsers.add_parser(
        "run",
        help="Run a workflow.",
        description="Run the Agent OS LangGraph workflow.",
    )
    run_parser.add_argument(
        "task", nargs="?", help="Task description for a new workflow."
    )
    run_parser.add_argument("--thread-id", help="Thread ID to create or resume.")
    run_parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume an interrupted workflow.",
    )
    run_parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Show node progress and tracebacks.",
    )
    run_parser.add_argument("--sandbox", help="Override AGENT_OS_SANDBOX for this run.")
    run_parser.add_argument("--profile", help="Named configuration profile to use.")
    run_parser.add_argument(
        "--workspace",
        default=argparse.SUPPRESS,
        help="Path to workspace.toml or a workspace directory.",
    )
    run_parser.add_argument(
        "--force-rebind",
        action="store_true",
        help="Replace a persisted backend binding when resuming.",
    )

    p_chat = subparsers.add_parser(
        "chat", help="Start an interactive conversational loop"
    )
    p_chat.add_argument(
        "--thread-id",
        help="Thread ID to persist checkpoint state. Generated if absent.",
    )
    p_chat.add_argument(
        "--resume", action="store_true", help="Resume from the last checkpoint."
    )
    p_chat.add_argument(
        "-v", "--verbose", action="store_true", help="Show node progress."
    )
    p_chat.add_argument("--sandbox", help="Override AGENT_OS_SANDBOX for this run.")
    p_chat.add_argument("--profile", help="Named configuration profile to use.")
    p_chat.add_argument(
        "--workspace",
        default=argparse.SUPPRESS,
        help="Path to workspace.toml or a workspace directory.",
    )
    p_chat.add_argument(
        "--force-rebind",
        action="store_true",
        help="Replace a persisted backend binding when resuming.",
    )

    p_sessions = subparsers.add_parser("sessions", help="Manage chat sessions")
    s_sub = p_sessions.add_subparsers(dest="session_command")
    s_sub.add_parser("list", help="List all sessions")

    s_inspect = s_sub.add_parser("inspect", help="Inspect a session")
    s_inspect.add_argument("thread_id", help="Thread ID")

    s_delete = s_sub.add_parser("delete", help="Delete a session")
    s_delete.add_argument("thread_id", help="Thread ID")

    s_resume = s_sub.add_parser("resume", help="Resume a session")
    s_resume.add_argument("thread_id", help="Thread ID")

    # agent-os serve
    serve_parser = subparsers.add_parser(
        "serve", help="Start the FastAPI server for the agent-os dashboard."
    )
    serve_parser.add_argument(
        "--host", default="127.0.0.1", help="Host to bind to (default: 127.0.0.1)"
    )
    serve_parser.add_argument(
        "--port", type=int, default=4680, help="Port to bind to (default: 4680)"
    )
    serve_parser.add_argument("--profile", help="Name of the profile to use")
    serve_parser.add_argument(
        "--workspace",
        default=argparse.SUPPRESS,
        help="Path to workspace.toml or a workspace directory.",
    )

    doctor_parser = subparsers.add_parser(
        "doctor", help="Check configuration and health."
    )
    doctor_parser.add_argument(
        "--json", dest="json_output", action="store_true", help="Output as JSON."
    )
    doctor_parser.add_argument(
        "--workspace",
        help="Validate backend bindings from this workspace.toml or workspace directory.",
    )

    update_parser = subparsers.add_parser(
        "update", help="Check and apply AgentOS updates."
    )
    update_parser.add_argument(
        "--check",
        action="store_true",
        help="Only check for available updates without applying.",
    )
    update_parser.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="Automatically accept update prompts.",
    )
    update_parser.add_argument(
        "--pull",
        action="store_true",
        help="Run docker compose pull if in Docker runtime with --yes.",
    )
    update_parser.add_argument(
        "--reload",
        action="store_true",
        help="Automatically kickstart daemon service after update.",
    )
    update_parser.add_argument(
        "--force",
        action="store_true",
        help="Force update even if version is current.",
    )

    p_brief = subparsers.add_parser("brief", help="Generate Morning Brief")
    p_brief.add_argument("--date", help="Target date YYYY-MM-DD")
    p_brief.add_argument("--profile", help="Named configuration profile to use.")
    p_brief.add_argument("--connector", help="Memory connector to use")
    p_brief.add_argument(
        "--workspace",
        default=argparse.SUPPRESS,
        help="Path to workspace.toml or a workspace directory.",
    )

    # agent-os schedule add|list|remove|run-once
    p_schedule = subparsers.add_parser("schedule", help="Manage schedules")
    sched_sub = p_schedule.add_subparsers(dest="schedule_command")

    p_sched_add = sched_sub.add_parser("add", help="Create a new schedule")
    p_sched_add.add_argument(
        "--name", required=True, help="Human-readable schedule name"
    )
    p_sched_add.add_argument(
        "--kind",
        required=True,
        choices=["run", "brief", "app_callback"],
        help="Schedule kind",
    )
    p_sched_add.add_argument("--cron", help="Five-field POSIX cron expression")
    p_sched_add.add_argument("--every", help="Interval (e.g. 30s, 5m, 2h, 1d)")
    p_sched_add.add_argument("--task", help="Task description (required for kind=run)")
    p_sched_add.add_argument(
        "--timezone", default="UTC", help="IANA timezone (default: UTC)"
    )
    p_sched_add.add_argument(
        "--workspace", help="Workspace path (optional for kind=run)"
    )
    p_sched_add.add_argument(
        "--url", help="Callback URL (required for kind=app_callback)"
    )
    p_sched_add.add_argument(
        "--method",
        choices=[
            "GET",
            "POST",
            "PUT",
            "PATCH",
            "DELETE",
            "get",
            "post",
            "put",
            "patch",
            "delete",
        ],
        help="HTTP method (default: POST)",
    )
    p_sched_add.add_argument("--headers", help="HTTP headers as JSON object string")
    p_sched_add.add_argument("--body", help="HTTP request body as JSON string")

    p_sched_list = sched_sub.add_parser("list", help="List schedules")
    p_sched_list.add_argument(
        "--kind", choices=["run", "brief", "app_callback"], help="Filter by kind"
    )
    p_sched_list.add_argument(
        "--enabled", choices=["true", "false"], help="Filter by enabled"
    )
    p_sched_list.add_argument(
        "--json", dest="json_output", action="store_true", help="Output as JSON"
    )

    p_sched_remove = sched_sub.add_parser("remove", help="Remove a schedule")
    p_sched_remove.add_argument("schedule_id", help="Schedule ID to remove")

    p_sched_runonce = sched_sub.add_parser(
        "run-once", help="Manually dispatch a schedule"
    )
    p_sched_runonce.add_argument("schedule_id", help="Schedule ID to dispatch")

    # agent-os permissions list|revoke
    p_permissions = subparsers.add_parser(
        "permissions", help="Manage learned permission rules"
    )
    perm_sub = p_permissions.add_subparsers(dest="permissions_command")

    p_perm_list = perm_sub.add_parser("list", help="List learned permission rules")
    p_perm_list.add_argument(
        "--json", dest="json_output", action="store_true", help="Output as JSON"
    )
    p_perm_list.add_argument(
        "--workspace",
        default=argparse.SUPPRESS,
        help="Path to workspace.toml or a workspace directory.",
    )

    p_perm_revoke = perm_sub.add_parser(
        "revoke", help="Revoke a learned permission rule"
    )
    p_perm_revoke.add_argument(
        "permission_key", help="Permission key to revoke (e.g., memory_write:write:*)"
    )
    p_perm_revoke.add_argument(
        "--workspace",
        default=argparse.SUPPRESS,
        help="Path to workspace.toml or a workspace directory.",
    )

    # agent-os memory index|status
    p_memory = subparsers.add_parser("memory", help="Manage memory index and status")
    mem_sub = p_memory.add_subparsers(dest="memory_command")

    p_mem_index = mem_sub.add_parser("index", help="Build or update the memory index")
    p_mem_index.add_argument(
        "refs", nargs="*", help="Optional specific note refs to index"
    )
    p_mem_index.add_argument(
        "--reindex", action="store_true", help="Force a full reindex"
    )
    p_mem_index.add_argument(
        "--json", dest="json_output", action="store_true", help="Output as JSON"
    )
    p_mem_index.add_argument(
        "--workspace",
        default=argparse.SUPPRESS,
        help="Path to workspace.toml or a workspace directory.",
    )

    p_mem_status = mem_sub.add_parser("status", help="Show memory index status")
    p_mem_status.add_argument(
        "--json", dest="json_output", action="store_true", help="Output as JSON"
    )
    p_mem_status.add_argument(
        "--workspace",
        default=argparse.SUPPRESS,
        help="Path to workspace.toml or a workspace directory.",
    )

    p_observations = subparsers.add_parser(
        "observations",
        help="Review structured outcome evidence (does not change agent policy).",
    )
    observation_sub = p_observations.add_subparsers(dest="observations_command")
    p_observation_list = observation_sub.add_parser(
        "list", help="List workspace observations"
    )
    p_observation_list.add_argument(
        "--json", dest="json_output", action="store_true", help="Output as JSON"
    )
    p_observation_list.add_argument(
        "--workspace",
        default=argparse.SUPPRESS,
        help="Path to workspace.toml or a workspace directory.",
    )
    p_observation_list.add_argument("--task-kind", help="Filter by task kind")
    p_observation_record = observation_sub.add_parser(
        "record-outcome",
        help="Record an explicit accepted, rejected, or edited outcome.",
    )
    p_observation_record.add_argument("observation_id", help="Observation ID")
    p_observation_record.add_argument(
        "--signal",
        required=True,
        choices=["accepted", "rejected", "edited"],
        help="Explicit operator outcome",
    )
    p_observation_record.add_argument(
        "--evidence", help="Short operator-supplied evidence"
    )
    p_observation_record.add_argument(
        "--workspace",
        default=argparse.SUPPRESS,
        help="Path to workspace.toml or a workspace directory.",
    )
    p_observation_assignment = observation_sub.add_parser(
        "assignment",
        help="Get strategy assignment audit record for a run.",
    )
    p_observation_assignment.add_argument("run_id", help="Run ID")
    p_observation_assignment.add_argument(
        "--json",
        dest="json_output",
        action="store_true",
        help="Output as JSON",
    )
    p_observation_assignment.add_argument(
        "--workspace",
        default=argparse.SUPPRESS,
        help="Path to workspace.toml or a workspace directory.",
    )

    # agent-os runs approvals <run_id>
    p_runs = subparsers.add_parser("runs", help="Inspect runs and audit history")
    runs_sub = p_runs.add_subparsers(dest="runs_command")
    p_runs_approvals = runs_sub.add_parser(
        "approvals", help="List approval and cancellation history for a run"
    )
    p_runs_approvals.add_argument("run_id", help="Run ID")
    p_runs_approvals.add_argument(
        "--json",
        dest="json_output",
        action="store_true",
        help="Output as JSON",
    )

    return parser


def _generate_thread_id() -> str:
    user = os.getenv("USER", "user").strip() or "user"
    timestamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"{user}-{timestamp}-{uuid.uuid4().hex[:8]}"


def _normalize_argv(argv: list[str]) -> list[str]:
    commands = (
        "run",
        "runs",
        "doctor",
        "update",
        "chat",
        "sessions",
        "serve",
        "brief",
        "schedule",
        "permissions",
        "observations",
        "memory",
    )
    if not argv:
        return ["run"]
    if argv[0] in commands:
        return argv
    if argv[0] == "--workspace" and len(argv) >= 2:
        if len(argv) >= 3 and argv[2] in commands:
            return argv
        return [argv[0], argv[1], "run", *argv[2:]]
    if argv[0].startswith("--workspace="):
        if len(argv) >= 2 and argv[1] in commands:
            return argv
        return [argv[0], "run", *argv[1:]]
    return ["run"] + argv


def _is_loopback_bind_host(host: str) -> bool:
    """Whether a server bind address is limited to this machine."""
    normalized = host.strip().lower()
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def _initial_state(
    task: str,
    backend_binding: BackendBinding,
    hot_context: str | None = None,
) -> dict[str, object]:
    return {
        "messages": [("user", task)],
        "task": task,
        "plan": None,
        "executor_output": None,
        "human_feedback": None,
        "hot_context": hot_context,
        "conversation_summary": None,
        "backend_binding": backend_binding,
    }


def _pending_interrupt(snapshot: Any) -> object | None:
    for task in getattr(snapshot, "tasks", ()):
        interrupts = getattr(task, "interrupts", ())
        if interrupts:
            return getattr(interrupts[0], "value", interrupts[0])
    return None


def _checkpoint_exists(snapshot: Any) -> bool:
    return bool(
        getattr(snapshot, "created_at", None) or getattr(snapshot, "values", None)
    )


def _next_node_name(snapshot: Any) -> str:
    next_nodes = tuple(getattr(snapshot, "next", ()) or ())
    return str(next_nodes[0]) if next_nodes else "unknown"


def _render_binding(binding: BackendBinding) -> str:
    return ", ".join(
        (
            f"router={binding.router!r}",
            f"architect={binding.architect!r}",
            f"executor={binding.executor!r}",
            f"profile_name={binding.profile_name!r}",
            f"sandbox_root={binding.sandbox_root!r}",
        )
    )


async def _prepare_resume_binding(
    graph: Any,
    config: dict[str, object],
    snapshot: Any,
    current: BackendBinding,
    force_rebind: bool,
    registry: Any,
    formatter: EventFormatter,
) -> tuple[Any | None, int | None]:
    from agent_os.bindings import binding_conflicts, validate_backend_binding

    values = getattr(snapshot, "values", None) or {}
    raw_persisted = values.get("backend_binding") if isinstance(values, dict) else None
    resume_node = _next_node_name(snapshot)

    if raw_persisted is None:
        if not force_rebind:
            formatter.print_error(
                "This checkpoint has no persisted backend binding (created before "
                "R1.2e). Resuming without an explicit backend selection is not "
                "safe. Pass --force-rebind to attach the current binding and "
                "continue. Prior backend selection is not inferred."
            )
            return None, 2
        try:
            validate_backend_binding(current, registry)
        except ValueError as error:
            formatter.print_error(f"Cannot force backend rebind: {error}")
            return None, 2
        formatter.print_warning(
            "Attaching current backend binding to a legacy checkpoint (no prior "
            f"binding present). New binding: {_render_binding(current)}. Current "
            f"resume node: {resume_node}."
        )
    else:
        try:
            persisted = BackendBinding.model_validate(raw_persisted)
        except ValueError as error:
            formatter.print_error(f"Persisted backend binding is invalid: {error}")
            return None, 2
        changes = binding_conflicts(persisted, current)
        if not changes:
            return snapshot, None
        if not force_rebind:
            rendered_changes = "; ".join(
                f"{field}: {old!r} -> {new!r}" for field, (old, new) in changes.items()
            )
            formatter.print_error(
                "Backend binding conflict. Persisted binding: "
                f"{_render_binding(persisted)}. Conflicting effective overrides: "
                f"{rendered_changes}. Edit the environment or profile to match, "
                "or pass --force-rebind."
            )
            return None, 2
        try:
            validate_backend_binding(current, registry)
        except ValueError as error:
            formatter.print_error(f"Cannot force backend rebind: {error}")
            return None, 2
        formatter.print_warning("Forcing backend rebind:")
        for field, (old, new) in changes.items():
            formatter.print_warning(f"{field}: {old!r} -> {new!r}")
        formatter.print_warning(f"Current resume node: {resume_node}")

    if resume_node == "executor":
        formatter.print_warning(
            "Partial edits may exist in the sandbox. Inspect before continuing."
        )
    await graph.aupdate_state(config, {"backend_binding": current})
    return await graph.aget_state(config), None


def _completed_with_tool_failure(snapshot: Any) -> bool:
    values = getattr(snapshot, "values", None) or {}
    if not isinstance(values, dict):
        return False
    tool_result = values.get("tool_result")
    return getattr(tool_result, "success", None) is False


def _is_llm_configuration_error(error: ValueError) -> bool:
    message = str(error).lower()
    indicators = (
        "model configured",
        "api_key",
        "api key",
        "api_base",
        "api base",
        "credentials",
    )
    return any(indicator in message for indicator in indicators)


def _print_resume_hint(formatter: EventFormatter, thread_id: str) -> None:
    formatter.print_info(
        f"Checkpoint preserved. Resume with: agent-os --resume --thread-id {thread_id}"
    )


def _read_feedback(
    prompt: object,
    formatter: EventFormatter,
    input_fn: InputFunction,
) -> str:
    from agent_os.nodes.human_gate import normalize_human_feedback
    from agent_os.policy import POLICY_APPROVAL_PROMPT_PREFIX, normalize_policy_feedback

    formatter.print_human_prompt(str(prompt))
    is_policy_prompt = str(prompt).startswith(POLICY_APPROVAL_PROMPT_PREFIX)
    normalizer = (
        normalize_policy_feedback if is_policy_prompt else normalize_human_feedback
    )
    guidance = (
        "Enter 'approved', 'session', 'always_approve', 'always_deny', or "
        "'rejected: <reason>'."
        if is_policy_prompt
        else "Enter 'approved', 'y', or 'rejected: <reason>'."
    )
    while True:
        raw_feedback = input_fn("> ")
        try:
            return normalizer(raw_feedback)
        except ValueError as error:
            formatter.print_error(f"Invalid feedback: {error}")
            formatter.print_info(guidance)


async def _stream_pass(
    graph: Any,
    graph_input: object,
    config: dict[str, object],
    formatter: EventFormatter,
    verbose: bool,
) -> None:
    from agent_os.policy import LocalPolicy, policy_scope

    configurable = config.get("configurable", {})
    if not isinstance(configurable, dict):
        configurable = {}
    thread_id = configurable.get("thread_id")
    session_key = str(thread_id) if isinstance(thread_id, str) and thread_id else None
    workspace_runtime = configurable.get("workspace")
    base_policy = getattr(workspace_runtime, "policy", None)
    if isinstance(base_policy, LocalPolicy):
        policy = base_policy.with_session(session_key)
    else:
        from agent_os.workspace import open_permission_store

        policy = LocalPolicy(store=open_permission_store(), session_key=session_key)

    # ContextVars propagate through the graph's async work, allowing a memory
    # write invoked deep in a runtime node to use this workspace/session policy.
    with policy_scope(policy):
        async for event in graph.astream_events(
            graph_input,
            config=config,
            version="v2",
        ):
            format_event(event, formatter, verbose=verbose)
    formatter.finish_stream()


async def _run_graph(
    graph: Any,
    *,
    task: str | None,
    resume: bool,
    verbose: bool,
    formatter: EventFormatter,
    input_fn: InputFunction,
    thread_id: str,
    backend_binding: BackendBinding,
    force_rebind: bool,
    registry: Any,
    workspace_runtime: object | None = None,
    initial_hot_context: str | None = None,
) -> int:
    from langgraph.types import Command

    from agent_os.routing import build_runtime_config

    config = build_runtime_config(thread_id)
    if workspace_runtime is not None:
        configurable = config.setdefault("configurable", {})
        if isinstance(configurable, dict):
            configurable["workspace"] = workspace_runtime

    if resume:
        snapshot = await graph.aget_state(config)
        if not _checkpoint_exists(snapshot):
            formatter.print_error(f"Checkpoint not found for thread '{thread_id}'.")
            return 2
        if not getattr(snapshot, "next", ()):
            formatter.print_error(
                f"Workflow '{thread_id}' is already finished and cannot be resumed."
            )
            return 2
        snapshot, binding_exit = await _prepare_resume_binding(
            graph,
            config,
            snapshot,
            backend_binding,
            force_rebind,
            registry,
            formatter,
        )
        if binding_exit is not None:
            return binding_exit
        assert snapshot is not None
        interrupt_prompt = _pending_interrupt(snapshot)
        if interrupt_prompt is not None:
            try:
                feedback = _read_feedback(interrupt_prompt, formatter, input_fn)
            except KeyboardInterrupt:
                formatter.print_error("Interrupted by Ctrl+C.")
                _print_resume_hint(formatter, thread_id)
                return 130
            except EOFError:
                formatter.print_error("Input closed while awaiting human feedback.")
                _print_resume_hint(formatter, thread_id)
                return 2
            graph_input: object = Command(resume=feedback)
        else:
            formatter.print_info(
                f"Resuming mid-run at node {_next_node_name(snapshot)}"
            )
            graph_input = None
    else:
        if task is None:
            raise AssertionError("A new workflow requires a task")
        graph_input = _initial_state(task, backend_binding, initial_hot_context)

    while True:
        try:
            await _stream_pass(
                graph,
                graph_input,
                config,
                formatter,
                verbose,
            )
            snapshot = await graph.aget_state(config)
        except KeyboardInterrupt:
            formatter.print_error("Interrupted by Ctrl+C.")
            _print_resume_hint(formatter, thread_id)
            return 130
        except EOFError:
            formatter.print_error("Input closed while awaiting human feedback.")
            _print_resume_hint(formatter, thread_id)
            return 2
        except ValueError as error:
            if _is_llm_configuration_error(error):
                formatter.print_error("Missing or invalid LLM configuration.")
                formatter.print_info(
                    "Configure LLM_ROUTER, LLM_ARCHITECT, and LLM_EXECUTOR; "
                    "see .env.example."
                )
            else:
                formatter.print_error(f"Workflow error: {error}")
            if verbose:
                traceback.print_exc(file=formatter.console.file)
            return 1
        except Exception as error:
            formatter.print_error(f"Workflow failed: {error}")
            if verbose:
                traceback.print_exc(file=formatter.console.file)
            return 1

        if not getattr(snapshot, "next", ()):
            if _completed_with_tool_failure(snapshot):
                return 1
            return 0

        interrupt_prompt = _pending_interrupt(snapshot)
        if interrupt_prompt is None:
            formatter.print_error(
                "Workflow paused at an unsupported node; checkpoint preserved."
            )
            _print_resume_hint(formatter, thread_id)
            return 1

        try:
            feedback = _read_feedback(interrupt_prompt, formatter, input_fn)
        except KeyboardInterrupt:
            formatter.print_error("Interrupted by Ctrl+C.")
            _print_resume_hint(formatter, thread_id)
            return 130
        except EOFError:
            formatter.print_error("Input closed while awaiting human feedback.")
            _print_resume_hint(formatter, thread_id)
            return 2
        graph_input = Command(resume=feedback)


async def _chat_loop(
    graph: Any,
    *,
    resume: bool,
    verbose: bool,
    formatter: EventFormatter,
    input_fn: InputFunction,
    thread_id: str,
    backend_binding: BackendBinding,
    force_rebind: bool,
    registry: Any,
    workspace_runtime: object | None = None,
    initial_hot_context: str | None = None,
) -> int:
    from langchain_core.messages import HumanMessage
    from langgraph.types import Command

    from agent_os.routing import build_runtime_config

    config = build_runtime_config(thread_id)
    if workspace_runtime is not None:
        configurable = config.setdefault("configurable", {})
        if isinstance(configurable, dict):
            configurable["workspace"] = workspace_runtime

    if resume:
        snapshot = await graph.aget_state(config)
        if not _checkpoint_exists(snapshot):
            formatter.print_error(f"Checkpoint not found for thread '{thread_id}'.")
            return 2
        snapshot, binding_exit = await _prepare_resume_binding(
            graph, config, snapshot, backend_binding, force_rebind, registry, formatter
        )
        if binding_exit is not None:
            return binding_exit
        assert snapshot is not None

        interrupt_prompt = _pending_interrupt(snapshot)
        if interrupt_prompt is not None:
            try:
                feedback = _read_feedback(interrupt_prompt, formatter, input_fn)
            except KeyboardInterrupt:
                formatter.print_error("Interrupted by Ctrl+C.")
                _print_resume_hint(formatter, thread_id)
                return 130
            except EOFError:
                formatter.print_error("Input closed while awaiting human feedback.")
                _print_resume_hint(formatter, thread_id)
                return 2
            graph_input = Command(resume=feedback)
            try:
                await _stream_pass(graph, graph_input, config, formatter, verbose)
            except KeyboardInterrupt:
                formatter.print_error("Interrupted by Ctrl+C.")
                _print_resume_hint(formatter, thread_id)
                return 130
            except EOFError:
                formatter.print_error("Input closed.")
                _print_resume_hint(formatter, thread_id)
                return 2
            except Exception as error:
                formatter.print_error(f"Workflow failed: {error}")
                if verbose:
                    traceback.print_exc(file=formatter.console.file)
                return 1

    while True:
        try:
            user_input = input_fn("> ")
            if user_input.strip() in ("/exit", "exit"):
                break
        except KeyboardInterrupt:
            formatter.print_error("Interrupted by Ctrl+C.")
            _print_resume_hint(formatter, thread_id)
            return 130
        except EOFError:
            break

        snapshot = await graph.aget_state(config)

        if not _checkpoint_exists(snapshot):
            # First turn: initialize state with the first message as task
            graph_input = _initial_state(
                user_input,
                backend_binding,
                initial_hot_context,
            )
        else:
            graph_input = {"messages": [HumanMessage(content=user_input)]}

        while True:
            try:
                await _stream_pass(
                    graph,
                    graph_input,
                    config,
                    formatter,
                    verbose,
                )
                snapshot = await graph.aget_state(config)
            except KeyboardInterrupt:
                formatter.print_error("Interrupted by Ctrl+C.")
                _print_resume_hint(formatter, thread_id)
                return 130
            except EOFError:
                formatter.print_error("Input closed while awaiting human feedback.")
                _print_resume_hint(formatter, thread_id)
                return 2
            except ValueError as error:
                if _is_llm_configuration_error(error):
                    formatter.print_error("Missing or invalid LLM configuration.")
                else:
                    formatter.print_error(f"Workflow error: {error}")
                if verbose:
                    traceback.print_exc(file=formatter.console.file)
                return 1
            except Exception as error:
                formatter.print_error(f"Workflow failed: {error}")
                if verbose:
                    traceback.print_exc(file=formatter.console.file)
                return 1

            if not getattr(snapshot, "next", ()):
                if _completed_with_tool_failure(snapshot):
                    return 1
                break

            interrupt_prompt = _pending_interrupt(snapshot)
            if interrupt_prompt is None:
                formatter.print_error(
                    "Workflow paused at an unsupported node; checkpoint preserved."
                )
                _print_resume_hint(formatter, thread_id)
                return 1

            try:
                feedback = _read_feedback(interrupt_prompt, formatter, input_fn)
            except KeyboardInterrupt:
                formatter.print_error("Interrupted by Ctrl+C.")
                _print_resume_hint(formatter, thread_id)
                return 130
            except EOFError:
                formatter.print_error("Input closed while awaiting human feedback.")
                _print_resume_hint(formatter, thread_id)
                return 2
            graph_input = Command(resume=feedback)

        from agent_os.principal import LocalPrincipalResolver
        from agent_os.sessions import upsert_session

        title = None
        if (
            not _checkpoint_exists(snapshot)
            and isinstance(graph_input, dict)
            and "messages" in graph_input
        ):
            msg = graph_input["messages"][0]
            content = msg[1] if isinstance(msg, tuple) else msg.content
            title = content[:50] + ("..." if len(content) > 50 else "")
        cli_principal = LocalPrincipalResolver().resolve()
        cli_ws = None
        if workspace_runtime is not None:
            ws_obj = getattr(workspace_runtime, "workspace", None)
            if ws_obj is not None:
                cli_ws = str(getattr(ws_obj, "base_path", ws_obj))
        upsert_session(
            thread_id,
            title,
            workspace_id=cli_ws,
            created_by=cli_principal.id,
        )

    # On exit, write session log if summary exists
    try:
        final_snapshot = await graph.aget_state(config)
        summary = final_snapshot.values.get("conversation_summary")
        if summary:
            import os

            from agent_os.connectors import GbrainConnector, MarkdownVaultConnector
            from agent_os.session_log import write_session_summary
            from agent_os.sessions import _get_db

            workspace_connector = getattr(workspace_runtime, "memory_connector", None)
            if workspace_connector is not None and hasattr(
                workspace_connector, "write_note"
            ):
                connector = workspace_connector
            else:
                from agent_os.server.runtime import composed_workspace

                runtime = composed_workspace()
                if runtime is not None and getattr(runtime, "memory_connector", None) is not None:
                    connector = runtime.memory_connector
                else:
                    connector_name = os.getenv("AGENT_OS_MEMORY_CONNECTOR", "markdown")
                    if connector_name == "gbrain":
                        connector = GbrainConnector()
                    else:
                        vault_path = os.getenv(
                            "AGENT_OS_VAULT_PATH", backend_binding.sandbox_root
                        )
                        connector = MarkdownVaultConnector(vault_path)

            with _get_db() as db:
                c = db.execute(
                    "SELECT turn_count, created_at, title FROM sessions WHERE thread_id = ?",
                    (thread_id,),
                )
                row = c.fetchone()

            session_meta = {
                "turn_count": row[0] if row else 0,
                "created_at": row[1] if row else "",
                "title": row[2] if row else "Untitled Session",
            }
            from agent_os.policy import LocalPolicy

            runtime_policy = getattr(workspace_runtime, "policy", None)
            if isinstance(runtime_policy, LocalPolicy):
                session_policy = runtime_policy.with_session(thread_id)
            else:
                from agent_os.workspace import open_permission_store

                session_policy = LocalPolicy(
                    store=open_permission_store(),
                    session_key=thread_id,
                )
            session_log_result = write_session_summary(
                connector,
                thread_id,
                summary,
                session_meta,
                engine=session_policy,
            )
            if not session_log_result.committed:
                formatter.print_warning(
                    "Session log was not saved: "
                    f"{session_log_result.error or 'write was not committed'}"
                )
    except Exception as e:
        formatter.print_warning(f"Failed to write session log: {e}")

    return 0


async def _handle_schedule_command(
    args: argparse.Namespace, formatter: EventFormatter
) -> int:
    """Handle all ``agent-os schedule`` subcommands."""
    import json as _json

    from agent_os.schedules import (
        create_schedule,
        get_schedule,
        list_schedules,
        remove_schedule,
    )

    if args.schedule_command == "add":
        from agent_os.schedule_models import ScheduleInput

        headers = None
        if getattr(args, "headers", None):
            try:
                headers = _json.loads(args.headers)
                if not isinstance(headers, dict):
                    formatter.print_error("--headers must be a JSON object")
                    return 2
            except _json.JSONDecodeError as exc:
                formatter.print_error(f"Invalid JSON for --headers: {exc}")
                return 2

        body = None
        if getattr(args, "body", None):
            try:
                body = _json.loads(args.body)
                if not isinstance(body, dict):
                    formatter.print_error("--body must be a JSON object")
                    return 2
            except _json.JSONDecodeError as exc:
                formatter.print_error(f"Invalid JSON for --body: {exc}")
                return 2

        try:
            schedule_input = ScheduleInput(
                name=args.name,
                kind=args.kind,
                cron=args.cron,
                every=args.every,
                timezone=args.timezone,
                task=args.task,
                workspace=args.workspace,
                url=getattr(args, "url", None),
                method=getattr(args, "method", None),
                headers=headers,
                body=body,
            )
        except (ValueError, Exception) as exc:
            formatter.print_error(str(exc))
            return 2

        try:
            sid = create_schedule(
                name=schedule_input.name,
                kind=schedule_input.kind,
                trigger_kind=schedule_input.trigger_kind,
                trigger_value=schedule_input.trigger_value,
                timezone=schedule_input.timezone,
                payload=schedule_input.payload,
            )
        except ValueError as exc:
            formatter.print_error(str(exc))
            return 2

        sched = get_schedule(sid)
        if sched:
            formatter.print_info(f"Created schedule {sid}")
            formatter.print_info(f"  Name: {sched['name']}")
            formatter.print_info(f"  Next run: {sched['next_run_at']}")
        return 0

    if args.schedule_command == "list":
        enabled = None
        if hasattr(args, "enabled") and args.enabled is not None:
            enabled = args.enabled == "true"
        kind = getattr(args, "kind", None)

        schedules = list_schedules(kind=kind, enabled=enabled)

        if getattr(args, "json_output", False):
            print(_json.dumps(schedules, indent=2, default=str))
            return 0

        if not schedules:
            formatter.print_info("No schedules found.")
            return 0

        for s in schedules:
            trigger = (
                f"cron={s['trigger_value']}"
                if s["trigger_kind"] == "cron"
                else f"every={s['trigger_value']}"
            )
            enabled_str = "enabled" if s["enabled"] else "disabled"
            last = s.get("last_status") or "—"
            last_err = f" ({s['last_error']})" if s.get("last_error") else ""
            formatter.print_info(
                f"{s['schedule_id']}  {s['name']}  {s['kind']}  "
                f"{trigger}  tz={s['timezone']}  {enabled_str}  "
                f"next={s['next_run_at'][:19]}  last={last}{last_err}"
            )
        return 0

    if args.schedule_command == "remove":
        if remove_schedule(args.schedule_id):
            formatter.print_info(f"Removed schedule {args.schedule_id}")
            return 0
        formatter.print_error(f"Schedule {args.schedule_id} not found")
        return 2

    if args.schedule_command == "run-once":
        sched = get_schedule(args.schedule_id)
        if sched is None:
            formatter.print_error(f"Schedule {args.schedule_id} not found")
            return 2

        from agent_os.scheduler import dispatch_schedule

        result = await dispatch_schedule(sched, manual=True)
        if result.status == "error":
            formatter.print_error(f"Dispatch failed: {result.error}")
            return 1
        if result.run_id:
            formatter.print_info(f"Run {result.run_id}: {result.status}")
        elif result.kind == "app_callback":
            formatter.print_info(
                f"Callback {result.status} (status code: {result.ref})"
            )
        elif result.ref:
            formatter.print_info(f"Written: {result.ref}")
        return 0

    formatter.print_error("Unknown schedule subcommand. See: agent-os schedule --help")
    return 2


def _open_permissions_store_for_command(workspace_path: str | None) -> Any:
    """Open the same learned-permissions store selected for a workspace run."""
    from agent_os.permission_store import SqlitePermissionStore
    from agent_os.workspace import load_workspace, resolve_permission_db_path

    workspace = load_workspace(workspace_path) if workspace_path else None
    return SqlitePermissionStore(resolve_permission_db_path(workspace))


def _handle_permissions_command(
    args: argparse.Namespace, formatter: EventFormatter
) -> int:
    import json as _json
    from dataclasses import asdict

    try:
        store = _open_permissions_store_for_command(getattr(args, "workspace", None))
    except Exception as e:
        formatter.print_error(f"Failed to open permissions store: {e}")
        return 2

    if args.permissions_command == "list":
        try:
            rules = store.list()
        except Exception as e:
            formatter.print_error(f"Failed to list permissions: {e}")
            return 2

        if getattr(args, "json_output", False):
            print(_json.dumps([asdict(r) for r in rules], indent=2))
            return 0

        if not rules:
            formatter.print_info("No learned permission rules found.")
            return 0

        for r in rules:
            formatter.print_info(
                f"{r.permission_key}  [{r.effect}]  tier={r.tier_at_creation}  "
                f"approved={r.approve_count}  denied={r.deny_count}"
            )
        return 0

    if args.permissions_command == "revoke":
        perm_key = getattr(args, "permission_key", "")
        if perm_key:
            perm_key = perm_key.strip()
        if not perm_key:
            formatter.print_error("Permission key is required to revoke.")
            return 2
        try:
            rule = store.get(perm_key)
            if not rule:
                formatter.print_error(f"Permission rule '{perm_key}' not found.")
                return 1
            store.delete(perm_key)
            formatter.print_info(f"Revoked permission rule '{perm_key}'.")
            return 0
        except ValueError as e:
            formatter.print_error(f"Invalid permission key: {e}")
            return 2
        except Exception as e:
            formatter.print_error(f"Failed to revoke permission rule: {e}")
            return 2

    formatter.print_error(
        "Unknown permissions subcommand. See: agent-os permissions --help"
    )
    return 2


def _open_observations_store_for_command(workspace_path: str | None) -> tuple[Any, str]:
    from agent_os.observations import observation_workspace_id, open_observation_store
    from agent_os.workspace import load_workspace

    workspace = load_workspace(workspace_path) if workspace_path else None
    store = open_observation_store(workspace)
    if store is None:
        raise RuntimeError("Observations store is unavailable")
    return store, observation_workspace_id(workspace)


def _handle_observations_command(
    args: argparse.Namespace, formatter: EventFormatter
) -> int:
    import json as _json

    from agent_os.observations import ObservationValidationError

    try:
        store, workspace_id = _open_observations_store_for_command(
            getattr(args, "workspace", None)
        )
    except Exception as error:
        formatter.print_error(f"Failed to open observations store: {error}")
        return 2

    if args.observations_command == "list":
        try:
            observations = store.list(
                workspace_id=workspace_id,
                task_kind=getattr(args, "task_kind", None),
            )
        except Exception as error:
            formatter.print_error(f"Failed to list observations: {error}")
            return 2
        if getattr(args, "json_output", False):
            print(_json.dumps([item.to_dict() for item in observations], indent=2))
            return 0
        if not observations:
            formatter.print_info("No structured observations found.")
            return 0
        for item in observations:
            formatter.print_info(
                f"{item.observation_id}  [{item.outcome_signal}]  "
                f"{item.task_kind}  {item.approach}"
            )
        return 0

    if args.observations_command == "record-outcome":
        try:
            observation = store.get(args.observation_id)
            if observation is None or observation.workspace_id != workspace_id:
                formatter.print_error("Observation not found.")
                return 1
            updated = store.record_outcome(
                args.observation_id,
                signal=args.signal,
                evidence=args.evidence,
            )
            if updated is None:
                formatter.print_error("Observation not found.")
                return 1
            formatter.print_info(
                f"Recorded {updated.outcome_signal} outcome for {updated.observation_id}."
            )
            return 0
        except ObservationValidationError as error:
            formatter.print_error(f"Invalid outcome: {error}")
            return 2
        except Exception as error:
            formatter.print_error(f"Failed to record outcome: {error}")
            return 2

    if args.observations_command == "assignment":
        try:
            assignment = store.get_strategy_assignment(args.run_id)
            if assignment is None or assignment.workspace_id != workspace_id:
                formatter.print_error("Strategy assignment not found.")
                return 1
            if getattr(args, "json_output", False):
                print(_json.dumps(assignment.to_dict(), indent=2))
                return 0
            for key, value in assignment.to_dict().items():
                if key == "evidence_summary" and value is not None:
                    formatter.print_info(f"{key}: {_json.dumps(value)}")
                else:
                    formatter.print_info(f"{key}: {value}")
            return 0
        except Exception as error:
            formatter.print_error(f"Failed to get strategy assignment: {error}")
            return 2

    formatter.print_error(
        "Unknown observations subcommand. See: agent-os observations --help"
    )
    return 2


def _handle_runs_command(
    args: argparse.Namespace, formatter: EventFormatter
) -> int:
    import json as _json

    from agent_os.stores import SqliteRunStore

    store = SqliteRunStore()

    if getattr(args, "runs_command", None) == "approvals":
        run_id = getattr(args, "run_id", "")
        if not run_id:
            formatter.print_error("Run ID is required.")
            return 2

        run = store.get_run(run_id)
        if run is None:
            formatter.print_error(f"Run '{run_id}' not found.")
            return 1

        approvals = store.list_approvals(run_id)
        if getattr(args, "json_output", False):
            print(_json.dumps(approvals, indent=2))
            return 0

        if not approvals:
            formatter.print_info(f"No approval decisions recorded for run '{run_id}'.")
            return 0

        for decision in approvals:
            detail = (
                f"#{decision['seq']} [{decision['decision']}] "
                f"actor={decision['actor_id']} ({decision['actor_kind']}) "
                f"at={decision['decided_at']}"
            )
            if decision.get("reason"):
                detail += f" reason={decision['reason']}"
            if decision.get("on_behalf_of"):
                detail += f" on_behalf_of={decision['on_behalf_of']}"
            formatter.print_info(detail)
        return 0

    formatter.print_error("Unknown runs subcommand. See: agent-os runs --help")
    return 2


def _handle_memory_command(
    args: argparse.Namespace, formatter: EventFormatter
) -> int:
    import json as _json

    if not getattr(args, "memory_command", None):
        formatter.print_error("Missing memory command. Use: agent-os memory [index|status]")
        return 1

    workspace_path = getattr(args, "workspace", None)
    memory_conn = None
    try:
        if workspace_path:
            from agent_os.workspace import compose_workspace, load_workspace

            ws = load_workspace(workspace_path)
            composed = compose_workspace(ws)
            memory_conn = composed.memory_connector
        else:
            from agent_os.server.runtime import composed_workspace, memory_connector

            runtime = composed_workspace()
            if runtime is not None:
                memory_conn = runtime.memory_connector
            else:
                memory_conn = memory_connector()
    except Exception as exc:
        if getattr(args, "json_output", False):
            print(_json.dumps({"error": f"Failed to load workspace: {exc}"}))
        else:
            formatter.print_error(f"Failed to load workspace: {exc}")
        return 1

    if memory_conn is None:
        if getattr(args, "json_output", False):
            print(_json.dumps({"error": "No memory connector available."}))
        else:
            formatter.print_error("No memory connector available.")
        return 1

    conn_name = getattr(memory_conn, "name", "unknown")
    if not (
        hasattr(memory_conn, "index")
        and callable(getattr(memory_conn, "index", None))
        and hasattr(memory_conn, "index_status")
        and callable(getattr(memory_conn, "index_status", None))
    ):
        if getattr(args, "json_output", False):
            print(
                _json.dumps(
                    {"error": f"Memory connector '{conn_name}' does not support indexing."}
                )
            )
        else:
            formatter.print_error(
                f"Memory connector '{conn_name}' does not support indexing."
            )
        return 1

    if args.memory_command == "index":
        try:
            if getattr(args, "reindex", False):
                result = memory_conn.reindex()
            else:
                refs = getattr(args, "refs", None) or None
                result = memory_conn.index(refs=refs)
        except Exception as exc:
            if getattr(args, "json_output", False):
                print(_json.dumps({"error": f"Indexing failed: {exc}"}))
            else:
                formatter.print_error(f"Indexing failed: {exc}")
            return 1

        if getattr(args, "json_output", False):
            payload = (
                result.model_dump()
                if hasattr(result, "model_dump")
                else {
                    "indexed_count": getattr(result, "indexed_count", 0),
                    "deleted_count": getattr(result, "deleted_count", 0),
                    "errors": getattr(result, "errors", []),
                    "metadata": getattr(result, "metadata", {}),
                }
            )
            print(_json.dumps(payload, indent=2))
        else:
            formatter.print_info(
                f"Memory index complete: {result.indexed_count} indexed, "
                f"{result.deleted_count} deleted, {len(result.errors)} errors."
            )
            if result.errors:
                for err in result.errors:
                    formatter.print_warning(f"  - {err}")
        return 0 if not result.errors else (0 if result.indexed_count > 0 else 1)

    elif args.memory_command == "status":
        try:
            status_data = memory_conn.index_status()
        except Exception as exc:
            if getattr(args, "json_output", False):
                print(_json.dumps({"error": f"Failed to get index status: {exc}"}))
            else:
                formatter.print_error(f"Failed to get index status: {exc}")
            return 1

        if getattr(args, "json_output", False):
            print(_json.dumps(status_data, indent=2))
        else:
            formatter.print_info(f"Memory Connector: {conn_name}")
            for k, v in sorted(status_data.items()):
                formatter.print_info(f"  {k}: {v}")
        return 0

    return 1


async def async_main(
    argv: list[str] | None = None,
    *,
    graph_factory: GraphFactory | None = None,
    input_fn: InputFunction = input,
    console: Console | None = None,
) -> int:
    """Parse arguments, construct the graph lazily, and run one workflow."""
    if argv is None:
        argv = sys.argv[1:]

    # Normalize argv: if first meaningful token isn't a command, prepend "run"
    # This also routes naked -h/--help to 'run --help' to preserve legacy help visibility.
    argv = _normalize_argv(argv)

    args = build_parser().parse_args(argv)

    if args.command == "memory":
        return _handle_memory_command(args, EventFormatter(console=console))

    if args.command == "runs":
        return _handle_runs_command(args, EventFormatter(console=console))

    if args.command == "permissions":
        return _handle_permissions_command(args, EventFormatter(console=console))

    if args.command == "observations":
        return _handle_observations_command(args, EventFormatter(console=console))

    if args.command == "sessions":
        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

        from agent_os.checkpoints import CHECKPOINT_DB_ENV, DEFAULT_CHECKPOINT_DB
        from agent_os.sessions import delete_session, get_session, list_sessions

        formatter = EventFormatter(console=console)
        if args.session_command == "list":
            sessions = list_sessions()
            for s in sessions:
                formatter.print_info(
                    f"ID: {s['thread_id']} | Turns: {s['turn_count']} | Last: {s['last_turn_at'][:16]} | {s['title']}"
                )
            return 0
        elif args.session_command == "resume":
            # Delegate to chat
            args.command = "chat"
            args.resume = True
            args.thread_id = args.thread_id
            args.task = None
            args.verbose = False
            args.sandbox = None
            args.profile = None
            args.force_rebind = False
        elif args.session_command == "inspect":
            session = get_session(args.thread_id)
            if not session:
                formatter.print_error(f"Session {args.thread_id} not found in index.")
                return 1
            formatter.print_info(f"Session: {args.thread_id}")
            formatter.print_info(f"Title: {session['title']}")
            formatter.print_info(f"Turns: {session['turn_count']}")
            return 0
        elif args.session_command == "delete":
            session = get_session(args.thread_id)
            if not session:
                formatter.print_error(f"Session {args.thread_id} not found.")
                return 1
            formatter.print_human_prompt(
                f"Are you sure you want to delete session '{args.thread_id}'? This will permanently delete the checkpoint. (y/N)"
            )
            ans = input_fn("> ")
            if ans.lower() in ("y", "yes"):
                database_path = os.getenv(CHECKPOINT_DB_ENV, DEFAULT_CHECKPOINT_DB)
                async with AsyncSqliteSaver.from_conn_string(database_path) as saver:
                    await saver.adelete_thread(args.thread_id)
                delete_session(args.thread_id)
                formatter.print_info(f"Session {args.thread_id} deleted.")
                return 0
            else:
                formatter.print_info("Deletion cancelled.")
                return 0

    if args.command == "schedule":
        return await _handle_schedule_command(args, EventFormatter(console=console))

    if args.command == "brief":
        from agent_os.backends import build_default_registry
        from agent_os.brief_runtime import execute_brief
        from agent_os.policy import LocalPolicy
        from agent_os.profiles import (
            load_profiles,
            resolve_profile,
            select_profile_name,
        )
        from agent_os.sandbox import get_sandbox_root

        # Resolve profile to set LLM_ARCHITECT
        try:
            profile_file = load_profiles()
            cli_name = getattr(args, "profile", None)
            env_name = os.getenv("AGENT_OS_PROFILE")
            file_default = profile_file.default
            profile_name, _ = select_profile_name(cli_name, env_name, file_default)
            registry = build_default_registry()
            if profile_name is not None:
                resolved_prof = resolve_profile(
                    profile_file, profile_name, registry, get_sandbox_root().resolve()
                )
                if resolved_prof and resolved_prof.architect:
                    os.environ["LLM_ARCHITECT"] = resolved_prof.architect
        except Exception:
            pass

        workspace_runtime = None
        session_key = f"brief:{uuid.uuid4()}"
        try:
            if getattr(args, "workspace", None):
                from agent_os.workspace import compose_workspace, load_workspace

                workspace_runtime = compose_workspace(load_workspace(args.workspace))
            if workspace_runtime is not None:
                policy = workspace_runtime.policy.with_session(session_key)
            else:
                from agent_os.workspace import open_permission_store

                policy = LocalPolicy(
                    store=open_permission_store(),
                    session_key=session_key,
                )
            res = execute_brief(
                date=getattr(args, "date", None),
                connector_name=getattr(args, "connector", None),
                connector=(
                    workspace_runtime.memory_connector if workspace_runtime else None
                ),
                write=True,
                engine=policy,
            )
        finally:
            LocalPolicy.clear_session(session_key)
        if res.saved:
            print(f"Morning Brief generated and saved to: {res.ref}")
        else:
            print(
                f"Morning Brief was generated but not saved: "
                f"{res.error or 'brief write was not committed'}",
                file=sys.stderr,
            )
        print(res.content)
        return 0 if res.saved else 1

    if args.command == "serve":
        previous_env = {
            "LLM_ROUTER": os.environ.get("LLM_ROUTER"),
            "LLM_ARCHITECT": os.environ.get("LLM_ARCHITECT"),
            "LLM_EXECUTOR": os.environ.get("LLM_EXECUTOR"),
            "AGENT_OS_MEMORY_CONNECTOR": os.environ.get("AGENT_OS_MEMORY_CONNECTOR"),
            "AGENT_OS_VAULT_PATH": os.environ.get("AGENT_OS_VAULT_PATH"),
            "AGENT_OS_WORKSPACE": os.environ.get("AGENT_OS_WORKSPACE"),
        }
        try:
            import uvicorn

            if args.workspace:
                from agent_os.workspace import compose_workspace, load_workspace

                composed_workspace = compose_workspace(load_workspace(args.workspace))
                for key, value in composed_workspace.environment.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
                os.environ["AGENT_OS_WORKSPACE"] = str(
                    composed_workspace.workspace.base_path / "workspace.toml"
                )
            from agent_os.server.api import EXECUTION_TOKEN_ENV
            from agent_os.server.api import app as fastapi_app
        except ImportError:
            print(
                "FastAPI dependencies not installed. Please run: pip install agent-os-langgraph[serve]"
            )
            return 1
        except Exception as error:
            print(f"Workspace error: {error}")
            return 2

        # We also need to set the profile if provided so the server uses it for backend_binding
        if args.profile and not args.workspace:
            os.environ["AGENT_OS_PROFILE"] = args.profile

        if (
            not _is_loopback_bind_host(args.host)
            and not os.getenv(EXECUTION_TOKEN_ENV, "").strip()
        ):
            print(
                "Refusing to bind the execution API outside localhost without "
                "AGENT_OS_EXECUTION_TOKEN."
            )
            return 2

        try:
            # We are already inside a running asyncio loop (async_main), so
            # uvicorn.run() — which starts its own loop — would raise "Cannot run
            # the event loop while another loop is running". Await a Server on the
            # current loop instead.
            config = uvicorn.Config(fastapi_app, host=args.host, port=args.port)
            await uvicorn.Server(config).serve()
            return 0
        finally:
            for key, value in previous_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    if args.command == "doctor":
        from agent_os.cli.doctor import run_doctor

        exit_code, output = run_doctor(args.json_output, args.workspace)
        print(output)
        return exit_code

    if args.command == "update":
        from agent_os.cli.update import handle_update_command

        return handle_update_command(args, console=console)

    formatter = EventFormatter(console=console)

    if args.command == "run":
        if args.resume and args.task:
            formatter.print_error("Do not provide a task when using --resume.")
            return 2
        if not args.resume and not args.task:
            formatter.print_error("Task is required for a new workflow.")
            return 2
    if args.resume and not args.thread_id:
        formatter.print_error("--thread-id is required when using --resume.")
        return 2
    if args.force_rebind and not args.resume:
        formatter.print_error("--force-rebind flag requires --resume.")
        return 2

    thread_id = args.thread_id or _generate_thread_id()
    formatter.print_thread_id(thread_id)

    # Resolve profile or workspace
    from agent_os.backends import build_default_registry
    from agent_os.profiles import load_profiles, resolve_profile, select_profile_name
    from agent_os.sandbox import get_sandbox_root

    composed_workspace = None
    workspace_runtime = None
    initial_hot_context = None
    workspace_env: dict[str, str | None] = {}
    registry = build_default_registry()
    profile_name = None
    resolved_prof = None

    try:
        if args.workspace:
            from agent_os.workspace import compose_workspace, load_workspace

            composed_workspace = compose_workspace(load_workspace(args.workspace))
            workspace_env = dict(composed_workspace.environment)
            profile_name = composed_workspace.backend_binding.profile_name
            initial_hot_context = composed_workspace.hot_context
            workspace_runtime = composed_workspace
            registry = composed_workspace.backend_registry
        else:
            profile_file = load_profiles()
            cli_name = args.profile
            env_name = os.getenv("AGENT_OS_PROFILE")
            file_default = profile_file.default

            profile_name, _ = select_profile_name(cli_name, env_name, file_default)
            if profile_name is not None:
                resolved_prof = resolve_profile(
                    profile_file, profile_name, registry, get_sandbox_root().resolve()
                )
    except Exception as e:
        prefix = "Workspace error" if args.workspace else "Profile error"
        formatter.print_error(f"{prefix}: {e}")
        return 2

    previous_env = {
        "LLM_ROUTER": os.environ.get("LLM_ROUTER"),
        "LLM_ARCHITECT": os.environ.get("LLM_ARCHITECT"),
        "LLM_EXECUTOR": os.environ.get("LLM_EXECUTOR"),
        "AGENT_OS_SANDBOX": os.environ.get("AGENT_OS_SANDBOX"),
        "AGENT_OS_MEMORY_CONNECTOR": os.environ.get("AGENT_OS_MEMORY_CONNECTOR"),
        "AGENT_OS_VAULT_PATH": os.environ.get("AGENT_OS_VAULT_PATH"),
        "AGENT_OS_WORKSPACE": os.environ.get("AGENT_OS_WORKSPACE"),
    }

    if composed_workspace is not None:
        for key, value in workspace_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        os.environ["AGENT_OS_WORKSPACE"] = str(
            composed_workspace.workspace.base_path / "workspace.toml"
        )

    if resolved_prof is not None:
        profile_env = {
            "LLM_ROUTER": resolved_prof.router,
            "LLM_ARCHITECT": resolved_prof.architect,
            "LLM_EXECUTOR": resolved_prof.executor,
            "AGENT_OS_SANDBOX": resolved_prof.sandbox,
        }
        for key, value in profile_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    if args.sandbox:
        os.environ["AGENT_OS_SANDBOX"] = args.sandbox

    # LiteLLM otherwise performs an HTTP fetch while importing model metadata.
    os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")

    try:
        from agent_os.bindings import resolve_backend_binding

        if composed_workspace is not None:
            backend_binding = composed_workspace.backend_binding
        else:
            backend_binding = resolve_backend_binding(profile_name)
        if graph_factory is not None:
            graph = graph_factory()
            if args.command == "chat":
                return await _chat_loop(
                    graph,
                    resume=args.resume,
                    verbose=args.verbose,
                    formatter=formatter,
                    input_fn=input_fn,
                    thread_id=thread_id,
                    backend_binding=backend_binding,
                    force_rebind=args.force_rebind,
                    registry=registry,
                    workspace_runtime=workspace_runtime,
                    initial_hot_context=initial_hot_context,
                )
            else:
                return await _run_graph(
                    graph,
                    task=args.task,
                    resume=args.resume,
                    verbose=args.verbose,
                    formatter=formatter,
                    input_fn=input_fn,
                    thread_id=thread_id,
                    backend_binding=backend_binding,
                    force_rebind=args.force_rebind,
                    registry=registry,
                    workspace_runtime=workspace_runtime,
                    initial_hot_context=initial_hot_context,
                )

        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

        from agent_os.checkpoints import (
            CHECKPOINT_DB_ENV,
            DEFAULT_CHECKPOINT_DB,
            get_checkpoint_serializer,
        )
        from agent_os.graph import build_graph
        from agent_os.nodes.tool_dispatcher import build_tool_dispatcher_node

        database_path = os.getenv(CHECKPOINT_DB_ENV, DEFAULT_CHECKPOINT_DB)
        async with AsyncSqliteSaver.from_conn_string(database_path) as saver:
            saver.serde = get_checkpoint_serializer()
            if composed_workspace is not None:
                graph = build_graph(
                    tool_dispatcher_node_impl=build_tool_dispatcher_node(
                        composed_workspace.skill_registry
                    ),
                    checkpointer=saver,
                )
            else:
                graph = build_graph(checkpointer=saver)
            if args.command == "chat":
                return await _chat_loop(
                    graph,
                    resume=args.resume,
                    verbose=args.verbose,
                    formatter=formatter,
                    input_fn=input_fn,
                    thread_id=thread_id,
                    backend_binding=backend_binding,
                    force_rebind=args.force_rebind,
                    registry=registry,
                    workspace_runtime=workspace_runtime,
                    initial_hot_context=initial_hot_context,
                )
            else:
                return await _run_graph(
                    graph,
                    task=args.task,
                    resume=args.resume,
                    verbose=args.verbose,
                    formatter=formatter,
                    input_fn=input_fn,
                    thread_id=thread_id,
                    backend_binding=backend_binding,
                    force_rebind=args.force_rebind,
                    registry=registry,
                    workspace_runtime=workspace_runtime,
                    initial_hot_context=initial_hot_context,
                )
    except KeyboardInterrupt:
        formatter.print_error("Interrupted by Ctrl+C.")
        _print_resume_hint(formatter, thread_id)
        return 130
    except Exception as error:
        formatter.print_error(f"Failed to initialize workflow: {error}")
        if args.verbose:
            traceback.print_exc(file=formatter.console.file)
        return 2
    finally:
        for k, v in previous_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        # Session grants exist only for the lifetime of this CLI invocation.
        from agent_os.policy import LocalPolicy

        LocalPolicy.clear_session(thread_id)


def main(argv: list[str] | None = None) -> int:
    """Synchronous console-script boundary."""
    try:
        return asyncio.run(async_main(argv))
    except KeyboardInterrupt:
        print("Interrupted by Ctrl+C.", file=sys.stderr)
        return 130
