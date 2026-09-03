import asyncio
import json
import os
from typing import Any

from langgraph.types import Command
from pydantic import BaseModel

from agent_os import runs
from agent_os.checkpoints import CHECKPOINT_DB_ENV, DEFAULT_CHECKPOINT_DB
from agent_os.cli.app import _pending_interrupt
from agent_os.observations import (
    observation_workspace_id,
    open_observation_store,
    task_signature_for_input,
)
from agent_os.policy import LocalPolicy, policy_scope
from agent_os.runs import TERMINAL_RUN_STATUSES
from agent_os.sandbox import sandbox_scope
from agent_os.server.runtime import (
    build_runtime_graph,
    initial_state,
    runtime_config,
    runtime_policy_for_session,
)


def _is_node_event(event_type: object, name: object) -> bool:
    return (
        event_type in {"on_chain_start", "on_chain_end"}
        and isinstance(name, str)
        and name not in {"LangGraph", "__start__"}
    )


def _append_stream_event(run_id: str, event: dict[str, Any]) -> None:
    event_type = event.get("event")
    name = event.get("name")
    if _is_node_event(event_type, name):
        runs.append_event(run_id, "node", {"name": name, "event": event_type})
        return

    if event_type == "on_chat_model_stream":
        data = event.get("data")
        if not isinstance(data, dict):
            return
        chunk = data.get("chunk")
        content = getattr(chunk, "content", None)
        if content:
            runs.append_event(run_id, "token", {"content": content})


def _result_payload(snapshot: object) -> dict[str, Any]:
    values = getattr(snapshot, "values", None)
    if not isinstance(values, dict):
        return {}
    tool_result = values.get("tool_result")
    if isinstance(tool_result, BaseModel):
        tool_result = tool_result.model_dump(mode="json")
    if isinstance(tool_result, dict):
        payload = {"tool_result": tool_result}
    else:
        payload = {}

    executor_output = values.get("executor_output")
    if isinstance(executor_output, BaseModel):
        executor_output = executor_output.model_dump(mode="json")
    if isinstance(executor_output, dict) and isinstance(
        executor_output.get("self_check"), dict
    ):
        payload["self_check"] = executor_output["self_check"]
    return payload


def terminal_tool_failure(snapshot: object) -> tuple[str, str] | None:
    """Return a terminal ledger status and diagnostic for an unsuccessful tool.

    The dispatcher deliberately serializes native ``ExecutionResult`` objects
    into ``ToolExecutionResult.output``.  Preserve their cancellation/error
    semantics at the HTTP run boundary instead of marking every terminal graph
    snapshot as successful.
    """
    result = _result_payload(snapshot).get("tool_result")
    if not isinstance(result, dict) or result.get("success") is not False:
        return None

    tool = str(result.get("tool") or "tool")
    raw_output = result.get("output")
    status = "error"
    errors: list[str] = []
    if isinstance(raw_output, str):
        try:
            parsed = json.loads(raw_output)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            if parsed.get("status") == "cancelled":
                status = "cancelled"
            raw_errors = parsed.get("errors")
            if isinstance(raw_errors, list):
                errors = [str(item) for item in raw_errors if str(item).strip()]
        if not errors and raw_output.strip():
            errors = [raw_output]

    message = "; ".join(errors) or f"{tool} did not complete successfully"
    return status, message


def _capture_terminal_observation(
    run: dict[str, Any],
    snapshot: object | None,
    *,
    terminal_status: str,
) -> None:
    """Best-effort, privacy-bounded terminal evidence for later user review.

    This intentionally excludes the task, model output, tool arguments, and
    serialized tool output.  A terminal execution is not user acceptance, so
    every automatically captured record starts as ``unknown``.
    """
    result = (
        _result_payload(snapshot).get("tool_result") if snapshot is not None else None
    )
    tool = result.get("tool") if isinstance(result, dict) else None
    values = getattr(snapshot, "values", None)
    hint = values.get("strategy_hint") if isinstance(values, dict) else None
    hint_data = hint.model_dump() if isinstance(hint, BaseModel) else hint
    strategy_id = hint_data.get("strategy_id") if isinstance(hint_data, dict) else None
    strategy_task_kind = (
        hint_data.get("task_kind") if isinstance(hint_data, dict) else None
    )
    task_kind = str(strategy_task_kind or tool or "workflow")
    approach = str(
        strategy_id or (f"native_tool:{task_kind}" if tool else "graph_terminal")
    )
    try:
        from agent_os.server.runtime import composed_workspace

        runtime = composed_workspace()
        workspace = runtime.workspace if runtime is not None else None
        store = open_observation_store(workspace)
        if store is None:
            return
        store.create(
            workspace_id=observation_workspace_id(workspace),
            run_id=str(run["run_id"]),
            thread_id=str(run["thread_id"]),
            task_kind=task_kind,
            task_signature=run.get("task_signature")
            or task_signature_for_input(run.get("task")),
            approach=approach,
            outcome_signal="unknown",
            outcome_evidence=f"terminal_status={terminal_status}",
            source="server.run",
        )
    except Exception:
        # Observation evidence must never change a run's execution outcome.
        return


async def execute_run(
    run_id: str,
    thread_id: str,
    task: str,
    *,
    resume_feedback: str | None = None,
    strategy_override: str | None = None,
) -> None:
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    db_path = os.getenv(CHECKPOINT_DB_ENV, DEFAULT_CHECKPOINT_DB)
    config = runtime_config(thread_id)
    # A run can be interrupted and resumed through multiple HTTP requests, so
    # the run id—not the process-global thread id—is the session grant boundary.
    session_policy = runtime_policy_for_session(run_id)
    preserve_session = False

    try:
        run = runs.get_run(run_id)
        if run is None:
            raise ValueError(f"Run {run_id} does not exist")
        runs.set_status(run_id, "running")
        with policy_scope(session_policy):
            with sandbox_scope(run.get("workspace")):
                async with AsyncSqliteSaver.from_conn_string(db_path) as saver:
                    # Match the ledger's busy_timeout so the async checkpointer waits for
                    # the shared-file write lock instead of failing with "database is
                    # locked" when the synchronous ledger is mid-write.
                    await saver.conn.execute("PRAGMA busy_timeout=30000")
                    graph = build_runtime_graph(checkpointer=saver)
                    graph_input: object
                    if resume_feedback is None:
                        graph_input = initial_state(
                            task,
                            run_id=run_id,
                            strategy_override=strategy_override,
                        )
                    else:
                        graph_input = Command(resume=resume_feedback)

                    async for event in graph.astream_events(
                        graph_input, config, version="v2"
                    ):
                        if isinstance(event, dict):
                            _append_stream_event(run_id, event)

                    snapshot = await graph.aget_state(config)
                    interrupt_prompt = _pending_interrupt(snapshot)
                    from agent_os.events import emit_run_lifecycle_event

                    workspace_id = str(
                        run.get("workspace_id") or run.get("workspace") or "default"
                    )

                    if interrupt_prompt is not None:
                        runs.append_event(
                            run_id,
                            "interrupt",
                            {"prompt": str(interrupt_prompt)},
                        )
                        runs.set_status(run_id, "interrupted")
                        emit_run_lifecycle_event(
                            "run.interrupted",
                            run_id=run_id,
                            workspace_id=workspace_id,
                            status="interrupted",
                            run=run,
                            thread_id=thread_id,
                        )
                        preserve_session = True
                    else:
                        result_payload = _result_payload(snapshot)
                        runs.append_event(run_id, "result", result_payload)
                        failure = terminal_tool_failure(snapshot)
                        if failure is None:
                            runs.set_status(run_id, "completed", ended=True)
                            emit_run_lifecycle_event(
                                "run.completed",
                                run_id=run_id,
                                workspace_id=workspace_id,
                                status="completed",
                                run=run,
                                thread_id=thread_id,
                            )
                            _capture_terminal_observation(
                                run, snapshot, terminal_status="completed"
                            )
                        else:
                            status, message = failure
                            runs.set_status(run_id, status, error=message, ended=True)
                            emit_run_lifecycle_event(
                                "run.cancelled" if status == "cancelled" else "run.failed",
                                run_id=run_id,
                                workspace_id=workspace_id,
                                status=status,
                                run=run,
                                thread_id=thread_id,
                            )
    except asyncio.CancelledError:
        run = runs.get_run(run_id)
        if run is not None and run["status"] not in TERMINAL_RUN_STATUSES:
            runs.set_status(run_id, "cancelled", ended=True)
            runs.append_event(run_id, "status", {"status": "cancelled"})
            workspace_id = str(
                run.get("workspace_id") or run.get("workspace") or "default"
            )
            from agent_os.events import emit_run_lifecycle_event

            emit_run_lifecycle_event(
                "run.cancelled",
                run_id=run_id,
                workspace_id=workspace_id,
                status="cancelled",
                run=run,
                thread_id=thread_id,
            )
        raise
    except Exception as error:
        message = str(error)
        runs.append_event(run_id, "error", {"message": message})
        runs.set_status(run_id, "error", error=message, ended=True)
        run = runs.get_run(run_id)
        if run is not None:
            workspace_id = str(
                run.get("workspace_id") or run.get("workspace") or "default"
            )
            from agent_os.events import emit_run_lifecycle_event

            emit_run_lifecycle_event(
                "run.failed",
                run_id=run_id,
                workspace_id=workspace_id,
                status="error",
                run=run,
                thread_id=thread_id,
            )
            _capture_terminal_observation(run, None, terminal_status="error")
    finally:
        if not preserve_session:
            LocalPolicy.clear_session(run_id)
