import asyncio
import hmac
import ipaddress
import json
import logging
import os
import re
import sqlite3
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any

from fastapi import (
    FastAPI,
    HTTPException,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict

from agent_os.checkpoints import CHECKPOINT_DB_ENV, DEFAULT_CHECKPOINT_DB
from agent_os.connectors import MemoryConnector
from agent_os.observations import (
    TASK_SIGNATURE_PREFIX,
    ObservationValidationError,
    observation_workspace_id,
    open_observation_store,
)
from agent_os.principal import LocalPrincipalResolver
from agent_os.sandbox import resolve_workspace_root
from agent_os.schedule_models import ScheduleInput
from agent_os.scheduler import SchedulerService
from agent_os.server.run_executor import execute_run, terminal_tool_failure
from agent_os.server.runtime import (
    build_runtime_graph,
    initial_state,
    memory_connector,
    package_version,
    runtime_config,
    runtime_policy_for_session,
    runtime_summary,
    task_kind_for_input,
)
from agent_os.sessions import delete_session, list_sessions
from agent_os.skill_authoring import (
    CandidateAuthoringInput,
    SkillAuthoringError,
    SkillAuthoringService,
)
from agent_os.skill_candidates import mine_skill_candidates
from agent_os.skills import SkillRegistry
from agent_os.stores import SqliteRunStore
from agent_os.strategies import strategies_for
from agent_os.update_check import check_for_update, read_cached_update

logger = logging.getLogger(__name__)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """Start/stop the scheduler service around the app lifetime."""
    scheduler = SchedulerService()
    app.state.scheduler = scheduler
    scheduler.start()
    try:
        # Out-of-band update check (fail-closed, non-blocking)
        asyncio.create_task(
            asyncio.to_thread(check_for_update, SERVER_VERSION, force=False)
        )
    except Exception:
        pass
    try:
        yield
    finally:
        await scheduler.stop()
        try:
            from agent_os.server.runtime import composed_workspace

            runtime = composed_workspace()
            if runtime is not None:
                for sink in getattr(runtime, "event_sinks", ()):
                    if hasattr(sink, "close") and callable(sink.close):
                        sink.close()
        except Exception as exc:
            logger.warning("Error closing event sinks during shutdown: %s", exc)


SERVER_VERSION = package_version()

app = FastAPI(title="agent-os API", version=SERVER_VERSION, lifespan=_lifespan)

# The Runtime API is meant to be driven by a browser operator console on a
# different localhost port, so cross-origin requests must be allowed. Defaults
# cover the console dev/prod port; override with AGENT_OS_CORS_ORIGINS (comma
# separated) for other origins.
_default_cors = "http://127.0.0.1:4100,http://localhost:4100"
_cors_origins = [
    o.strip()
    for o in os.getenv("AGENT_OS_CORS_ORIGINS", _default_cors).split(",")
    if o.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)

TERMINAL_RUN_STATUSES = {"completed", "cancelled", "error"}
GRAPH_MAX_NODES = 200
ACTIVE_RUN_TASKS: dict[str, asyncio.Task[None]] = {}
PERMISSIONS_ADMIN_TOKEN_ENV = "AGENT_OS_PERMISSIONS_ADMIN_TOKEN"
EXECUTION_TOKEN_ENV = "AGENT_OS_EXECUTION_TOKEN"
WEBSOCKET_TOKEN_PROTOCOL_PREFIX = "agent-os-token."
WEBSOCKET_PROTOCOL = "agent-os"


class CreateRunRequest(BaseModel):
    task: str
    thread_id: str | None = None
    workspace: str | None = None
    strategy_id: str | None = None
    task_signature: str | None = None


class ApproveRunRequest(BaseModel):
    feedback: str | None = None


class RecordObservationOutcomeRequest(BaseModel):
    signal: str
    evidence: str | None = None


class PrepareSkillAuthoringRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    candidate: CandidateAuthoringInput


class SubmitSkillDraftRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    draft_path: str


class ConfirmSkillRegistrationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirm: bool


def _require_permissions_admin(request: Request) -> None:
    """Require an explicit operator token for permission-rule administration."""
    token = os.getenv(PERMISSIONS_ADMIN_TOKEN_ENV, "").strip()
    if not token:
        raise HTTPException(
            status_code=404,
            detail="Permissions administration is not configured",
        )

    provided = request.headers.get("x-admin-token", "").strip()
    bearer = request.headers.get("authorization", "").strip()
    if bearer.lower().startswith("bearer "):
        provided = bearer[7:].strip()
    if not hmac.compare_digest(provided, token):
        raise HTTPException(status_code=403, detail="Forbidden")


def _is_loopback_client(host: object) -> bool:
    """Return whether an ASGI peer is local to this host."""
    if not isinstance(host, str):
        return False
    normalized = host.strip().lower()
    # ``testclient`` is Starlette's in-process ASGI peer name, not a network
    # address a remote caller can present to a real Uvicorn server.
    if normalized in {"localhost", "testclient"}:
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def _requested_websocket_protocols(websocket: WebSocket) -> list[str]:
    return [
        protocol.strip()
        for protocol in websocket.headers.get("sec-websocket-protocol", "").split(",")
        if protocol.strip()
    ]


def _execution_token_from_connection(connection: Request | WebSocket) -> str:
    token = connection.headers.get("x-execution-token", "").strip()
    bearer = connection.headers.get("authorization", "").strip()
    if bearer.lower().startswith("bearer "):
        token = bearer[7:].strip()
    if token or not isinstance(connection, WebSocket):
        return token

    # Browsers cannot set arbitrary WebSocket headers. They can, however,
    # offer a token-bearing subprotocol. The server later selects only the
    # fixed ``agent-os`` protocol, so the secret is not reflected in the
    # response or placed in a URL/query string.
    for protocol in _requested_websocket_protocols(connection):
        if protocol.startswith(WEBSOCKET_TOKEN_PROTOCOL_PREFIX):
            return protocol.removeprefix(WEBSOCKET_TOKEN_PROTOCOL_PREFIX)
    return token


def _check_private_api_access(connection: Request | WebSocket) -> tuple[str | None, dict[str, Any]]:
    """Authorize private runtime endpoints and extract trusted auth state."""
    origin = connection.headers.get("origin", "").strip()
    if origin and origin not in _cors_origins:
        return "Origin is not allowed", {}

    required_token = os.getenv(EXECUTION_TOKEN_ENV, "").strip()
    client = connection.client
    client_host = client.host if client is not None else None
    is_loopback = _is_loopback_client(client_host)

    if not required_token:
        if is_loopback:
            return None, {"authenticated_with_token": False, "is_loopback": True}
        return (
            "Private execution API requires AGENT_OS_EXECUTION_TOKEN "
            "for non-loopback clients"
        ), {}

    supplied_token = _execution_token_from_connection(connection)
    if not hmac.compare_digest(supplied_token, required_token):
        return "Forbidden", {}
    return None, {"authenticated_with_token": True, "is_loopback": is_loopback}


def _private_api_access_error(connection: Request | WebSocket) -> str | None:
    """Authorize private runtime endpoints without exposing them to the web.

    Local CLI/dashboard use stays zero-config. Any non-loopback peer requires
    an explicit token; browser WebSocket/HTTP requests must additionally come
    from a configured operator origin, because CORS does not secure WebSocket
    handshakes and does not prevent cross-site form posts.
    """
    error, _ = _check_private_api_access(connection)
    return error


@app.middleware("http")
async def protect_private_api(request: Request, call_next: Any) -> Any:
    path = request.url.path
    if path.startswith("/api/") and not (
        path == "/api/health" or path.startswith("/api/public/")
    ):
        error, auth_state = _check_private_api_access(request)
        if error is not None:
            return JSONResponse(status_code=403, content={"detail": error})
        for key, val in auth_state.items():
            setattr(request.state, key, val)
    else:
        request.state.authenticated_with_token = False
        client = request.client
        request.state.is_loopback = _is_loopback_client(client.host if client else None)
    return await call_next(request)


def _runtime_permission_store() -> Any:
    """Open the store selected by the active workspace/runtime configuration."""
    from agent_os.permission_store import SqlitePermissionStore
    from agent_os.server.runtime import composed_workspace
    from agent_os.workspace import resolve_permission_db_path

    runtime = composed_workspace()
    workspace = runtime.workspace if runtime is not None else None
    return SqlitePermissionStore(resolve_permission_db_path(workspace))


def _runtime_observation_store() -> tuple[Any, str]:
    """Select the same workspace-scoped evidence store as the active runtime."""
    from agent_os.server.runtime import composed_workspace

    runtime = composed_workspace()
    workspace = runtime.workspace if runtime is not None else None
    store = open_observation_store(workspace)
    if store is None:
        raise RuntimeError("Observations store is unavailable")
    return store, observation_workspace_id(workspace)


def _runtime_skill_candidate_context() -> tuple[Any, str, SkillRegistry]:
    """Return only read-only inputs required by the candidate miner."""
    from agent_os.server.runtime import composed_workspace

    runtime = composed_workspace()
    workspace = runtime.workspace if runtime is not None else None
    store = open_observation_store(workspace)
    if store is None:
        raise RuntimeError("Observations store is unavailable")
    registry = runtime.skill_registry if runtime is not None else SkillRegistry()
    return store, observation_workspace_id(workspace), registry


def _runtime_skill_authoring_service() -> SkillAuthoringService:
    """Authoring is deliberately unavailable without a configured workspace."""
    from agent_os.server.runtime import composed_workspace

    runtime = composed_workspace()
    if runtime is None:
        raise RuntimeError("Skill authoring requires an active workspace")
    return SkillAuthoringService(runtime.workspace)


@app.get("/api/health")
def health_check() -> dict[str, Any]:
    workspace = runtime_summary()
    update_info = read_cached_update(current=SERVER_VERSION)
    return {
        "status": "ok" if workspace.get("status") != "error" else "degraded",
        "version": SERVER_VERSION,
        "current_version": update_info.current_version,
        "latest_version": update_info.latest_version,
        "update_available": update_info.update_available,
        "workspace": workspace,
        "active_runs": len(ACTIVE_RUN_TASKS),
    }


@app.post("/api/update/check")
def update_check_endpoint() -> dict[str, Any]:
    info = check_for_update(current=SERVER_VERSION, force=True)
    return info.to_dict()


@app.get("/api/sessions")
def get_sessions() -> list[dict[str, Any]]:
    return list_sessions()


@app.get("/api/sessions/{session_id}")
def get_session(session_id: str) -> dict[str, Any]:
    from langgraph.checkpoint.sqlite import SqliteSaver

    db_path = os.getenv(CHECKPOINT_DB_ENV, DEFAULT_CHECKPOINT_DB)
    try:
        conn = sqlite3.connect(db_path, check_same_thread=False)
        saver = SqliteSaver(conn)
        config = {"configurable": {"thread_id": session_id}}
        tup = saver.get_tuple(config)
        if tup is None:
            raise HTTPException(status_code=404, detail="Session not found")

        state_vals = tup.checkpoint.get("channel_values", {})
        messages = [
            {"type": type(m).__name__, "content": m.content}
            for m in state_vals.get("messages", [])
        ]
        state = {k: v for k, v in state_vals.items() if k != "messages"}
        return {"id": session_id, "messages": messages, "state": state}
    finally:
        if "conn" in locals():
            conn.close()


@app.delete("/api/sessions/{session_id}")
def remove_session(session_id: str, confirm: bool = False) -> dict[str, str]:
    if not confirm:
        raise HTTPException(status_code=400, detail="Missing confirm=true")
    from langgraph.checkpoint.sqlite import SqliteSaver

    db_path = os.getenv(CHECKPOINT_DB_ENV, DEFAULT_CHECKPOINT_DB)
    try:
        conn = sqlite3.connect(db_path, check_same_thread=False)
        saver = SqliteSaver(conn)
        # Assuming delete_thread exists on SqliteSaver in this setup, or we just rely on delete_session
        try:
            # LangGraph might not have delete_thread sync method on all backends, let's just ignore if not present
            if hasattr(saver, "delete_thread"):
                saver.delete_thread(session_id)
        except Exception:
            pass
        delete_session(session_id)
        return {"status": "deleted", "id": session_id}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e
    finally:
        if "conn" in locals():
            conn.close()


@app.post("/api/brief")
@app.get("/api/brief")
def create_brief() -> dict[str, str]:
    from agent_os.brief_runtime import execute_brief

    res = execute_brief(write=False)
    return {"date": res.date, "content": res.content}


def build_graph_data(
    connector: MemoryConnector,
    max_nodes: int,
) -> dict[str, list[dict[str, Any]]]:
    nodes: list[dict[str, Any]] = []
    for note in connector.list_notes()[: max(max_nodes, 0)]:
        ref = note.get("ref")
        if not ref:
            continue

        title = note.get("title")
        nodes.append(
            {
                "id": ref,
                "title": title or ref,
                "group": ref.split("/")[0] if "/" in ref else "",
                "type": "page",
            }
        )

    refs = {node["id"] for node in nodes}
    title_to_ref = {
        node["title"]: node["id"]
        for node in nodes
        if node["title"] and node["title"] != node["id"]
    }

    edges: list[dict[str, Any]] = []
    seen_edges: set[tuple[str, str]] = set()
    for node in nodes:
        source = node["id"]
        try:
            note = connector.read_note(source)
        except Exception:
            continue

        for link in note.get("links") or []:
            target = link if link in refs else title_to_ref.get(link)
            if target not in refs or target == source:
                continue

            edge_key = (source, target)
            if edge_key in seen_edges:
                continue
            seen_edges.add(edge_key)
            edges.append({"source": source, "target": target, "type": "link"})

    return {"nodes": nodes, "edges": edges}


@app.get("/api/graph")
def get_graph_data(limit: int = GRAPH_MAX_NODES) -> dict[str, list[dict[str, Any]]]:
    try:
        effective_max = min(max(limit, 0), GRAPH_MAX_NODES)
        return build_graph_data(memory_connector(), effective_max)
    except Exception:
        return {"nodes": [], "edges": []}


_run_store = SqliteRunStore()


def _start_run_task(
    run_id: str,
    thread_id: str,
    task: str,
    *,
    resume_feedback: str | None = None,
    strategy_override: str | None = None,
) -> None:
    existing = ACTIVE_RUN_TASKS.get(run_id)
    if existing is not None and not existing.done():
        return

    async def runner() -> None:
        try:
            run_kwargs: dict[str, str | None] = {"resume_feedback": resume_feedback}
            if strategy_override is not None:
                run_kwargs["strategy_override"] = strategy_override
            await execute_run(run_id, thread_id, task, **run_kwargs)
        except asyncio.CancelledError:
            run = _run_store.get_run(run_id)
            if run is not None and run["status"] not in TERMINAL_RUN_STATUSES:
                _run_store.set_status(run_id, "cancelled", ended=True)
                _run_store.append_event(run_id, "status", {"status": "cancelled"})
            raise
        finally:
            ACTIVE_RUN_TASKS.pop(run_id, None)

    ACTIVE_RUN_TASKS[run_id] = asyncio.create_task(
        runner(),
        name=f"agent-os-run-{run_id}",
    )


@app.post("/api/runs")
async def create_run_endpoint(
    request: CreateRunRequest,
    http_request: Request,
) -> dict[str, str]:
    if request.task_signature is not None and not re.fullmatch(
        rf"{re.escape(TASK_SIGNATURE_PREFIX)}[0-9a-f]{{64}}", request.task_signature
    ):
        raise HTTPException(
            status_code=422, detail="task_signature must be a sha256:v1 digest"
        )
    if request.strategy_id is not None:
        allowed = {
            item.strategy_id
            for item in strategies_for(task_kind_for_input(request.task))
        }
        if request.strategy_id not in allowed:
            raise HTTPException(
                status_code=422, detail="Strategy is not allowed for this task kind"
            )
    try:
        workspace = str(resolve_workspace_root(request.workspace))
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    thread_id = request.thread_id or str(uuid.uuid4())
    principal = LocalPrincipalResolver().resolve(http_request)
    run_id = _run_store.create_run(
        thread_id,
        workspace,
        request.task,
        task_signature=request.task_signature,
        workspace_id=workspace,
        principal=principal,
    )
    from agent_os.events import emit_run_lifecycle_event

    emit_run_lifecycle_event(
        "run.created",
        run_id=run_id,
        workspace_id=workspace,
        status="queued",
        principal=principal,
        thread_id=thread_id,
    )
    _start_run_task(
        run_id,
        thread_id,
        request.task,
        strategy_override=request.strategy_id,
    )
    return {"run_id": run_id, "thread_id": thread_id, "status": "queued"}


@app.get("/api/runs")
def list_runs_endpoint(
    status: str | None = None,
    workspace: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    return [
        _with_result_self_check(run)
        for run in _run_store.list_runs(status=status, workspace=workspace, limit=limit)
    ]


def _latest_result_self_check(run_id: str) -> dict[str, Any] | None:
    for event in reversed(_run_store.list_events(run_id)):
        if event["kind"] != "result" or not isinstance(event["payload"], dict):
            continue
        self_check = event["payload"].get("self_check")
        if isinstance(self_check, dict):
            return self_check
    return None


def _with_result_self_check(run: dict[str, Any]) -> dict[str, Any]:
    enriched = dict(run)
    if enriched.get("status") == "completed":
        self_check = _latest_result_self_check(str(enriched["run_id"]))
        if self_check is not None:
            enriched["self_check"] = self_check
    return enriched


def _latest_interrupt_prompt(run_id: str) -> object | None:
    for event in reversed(_run_store.list_events(run_id)):
        if event["kind"] == "interrupt":
            payload = event["payload"]
            if isinstance(payload, dict):
                return payload.get("prompt")
            return payload
    return None


@app.get("/api/runs/{run_id}")
def get_run_endpoint(run_id: str) -> dict[str, Any]:
    run = _run_store.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    run = _with_result_self_check(run)
    run["interrupt"] = (
        _latest_interrupt_prompt(run_id) if run["status"] == "interrupted" else None
    )
    return run


@app.get("/api/runs/{run_id}/approvals")
def get_run_approvals_endpoint(run_id: str) -> list[dict[str, Any]]:
    run = _run_store.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    return _run_store.list_approvals(run_id)


@app.post("/api/runs/{run_id}/approve")
async def approve_run_endpoint(
    run_id: str,
    request: ApproveRunRequest,
    http_request: Request,
) -> dict[str, str]:
    run = _run_store.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    if run["status"] != "interrupted":
        if run["status"] == "running":
            return {"run_id": run_id, "status": "running"}
        raise HTTPException(status_code=409, detail="Run is not interrupted")

    principal = LocalPrincipalResolver().resolve(http_request)
    if not _run_store.record_approval_and_transition(
        run_id,
        decision="approved",
        actor_id=principal.id,
        actor_kind=principal.kind,
        on_behalf_of=principal.on_behalf_of,
        reason=request.feedback or None,
        expected="interrupted",
        status="running",
    ):
        latest = _run_store.get_run(run_id)
        if latest is not None and latest["status"] == "running":
            return {"run_id": run_id, "status": "running"}
        raise HTTPException(status_code=409, detail="Run is not interrupted")
    from agent_os.events import emit_run_lifecycle_event

    emit_run_lifecycle_event(
        "run.approved",
        run_id=run_id,
        workspace_id=str(run.get("workspace_id") or run.get("workspace") or "default"),
        status="running",
        principal=principal,
        thread_id=str(run.get("thread_id") or ""),
    )
    _start_run_task(
        run_id,
        run["thread_id"],
        "",
        resume_feedback=request.feedback or "",
    )
    return {"run_id": run_id, "status": "running"}


@app.post("/api/runs/{run_id}/cancel")
async def cancel_run_endpoint(run_id: str, request: Request) -> dict[str, str]:
    run = _run_store.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    if run["status"] in TERMINAL_RUN_STATUSES:
        raise HTTPException(status_code=409, detail="Run is already terminal")

    principal = LocalPrincipalResolver().resolve(request)
    if not _run_store.record_cancellation_and_transition(
        run_id,
        actor_id=principal.id,
        actor_kind=principal.kind,
        on_behalf_of=principal.on_behalf_of,
    ):
        latest = _run_store.get_run(run_id)
        if latest is None:
            raise HTTPException(status_code=404, detail="Run not found")
        raise HTTPException(status_code=409, detail="Run is already terminal")

    active_task = ACTIVE_RUN_TASKS.get(run_id)
    if active_task is not None and not active_task.done():
        active_task.cancel()
    _run_store.append_event(run_id, "status", {"status": "cancelled"})
    from agent_os.events import emit_run_lifecycle_event

    emit_run_lifecycle_event(
        "run.cancelled",
        run_id=run_id,
        workspace_id=str(run.get("workspace_id") or run.get("workspace") or "default"),
        status="cancelled",
        principal=principal,
        thread_id=str(run.get("thread_id") or ""),
    )
    from agent_os.policy import LocalPolicy

    LocalPolicy.clear_session(run_id)
    return {"run_id": run_id, "status": "cancelled"}


@app.get("/api/runs/{run_id}/events")
def get_run_events(run_id: str, after: int = 0) -> StreamingResponse:
    if _run_store.get_run(run_id) is None:
        raise HTTPException(status_code=404, detail="Run not found")

    async def event_stream():
        last_seq = after
        for event in _run_store.list_events(run_id, after=last_seq):
            last_seq = event["seq"]
            yield f"data: {json.dumps(event)}\n\n"

        while True:
            for event in _run_store.list_events(run_id, after=last_seq):
                last_seq = event["seq"]
                yield f"data: {json.dumps(event)}\n\n"

            run = _run_store.get_run(run_id)
            if run is None or run["status"] in TERMINAL_RUN_STATUSES:
                yield "event: end\ndata: {}\n\n"
                return

            await asyncio.sleep(0.5)

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@app.get("/api/schedules")
def list_schedules_endpoint(
    kind: str | None = None,
    enabled: bool | None = None,
) -> list[dict[str, Any]]:
    from agent_os.schedules import list_schedules

    return list_schedules(kind=kind, enabled=enabled)


@app.post("/api/schedules", status_code=201)
def create_schedule_endpoint(request: ScheduleInput) -> dict[str, Any]:
    from agent_os.schedules import create_schedule, get_schedule

    try:
        sid = create_schedule(
            name=request.name,
            kind=request.kind,
            trigger_kind=request.trigger_kind,
            trigger_value=request.trigger_value,
            timezone=request.timezone,
            payload=request.payload,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    sched = get_schedule(sid)
    if sched is None:
        raise HTTPException(
            status_code=500, detail="Failed to retrieve created schedule"
        )
    return sched


@app.get("/api/permissions")
def list_permissions_endpoint(request: Request) -> list[dict[str, Any]]:
    from dataclasses import asdict

    _require_permissions_admin(request)
    try:
        store = _runtime_permission_store()
        return [asdict(r) for r in store.list()]
    except Exception as exc:
        raise HTTPException(
            status_code=500, detail="Failed to read permissions store"
        ) from exc


@app.delete("/api/permissions/{permission_key:path}")
def revoke_permission_endpoint(permission_key: str, request: Request) -> dict[str, Any]:
    _require_permissions_admin(request)

    try:
        store = _runtime_permission_store()
        rule = store.get(permission_key)
        if not rule:
            raise HTTPException(
                status_code=404, detail=f"Permission rule '{permission_key}' not found"
            )
        store.delete(permission_key)
        return {"status": "ok", "revoked": permission_key}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=500, detail="Failed to revoke permission rule"
        ) from exc


@app.get("/api/observations")
def list_observations_endpoint(
    task_kind: str | None = None,
    limit: int = 100,
) -> list[dict[str, object]]:
    """List structured outcome evidence for the active workspace only."""
    try:
        store, workspace_id = _runtime_observation_store()
        return [
            observation.to_dict()
            for observation in store.list(
                workspace_id=workspace_id,
                task_kind=task_kind,
                limit=limit,
            )
        ]
    except Exception as exc:
        raise HTTPException(
            status_code=500, detail="Failed to read observations store"
        ) from exc


@app.get("/api/skill-candidates")
def list_skill_candidates_endpoint() -> dict[str, object]:
    """Read deterministic, review-only skill candidates for this workspace."""
    try:
        store, workspace_id, registry = _runtime_skill_candidate_context()
        candidates = mine_skill_candidates(store, registry, workspace_id=workspace_id)
        return {
            "workspace_id": workspace_id,
            "candidates": [
                candidate.model_dump(mode="json") for candidate in candidates
            ],
        }
    except Exception as exc:
        raise HTTPException(
            status_code=500, detail="Failed to mine skill candidates"
        ) from exc


@app.get("/api/skill-authoring/requests")
def list_skill_authoring_requests_endpoint() -> dict[str, object]:
    try:
        service = _runtime_skill_authoring_service()
        return {
            "requests": [
                request.model_dump(mode="json") for request in service.list_requests()
            ]
        }
    except Exception as exc:
        raise HTTPException(
            status_code=500, detail="Failed to read skill authoring requests"
        ) from exc


@app.post("/api/skill-authoring/requests")
def prepare_skill_authoring_request_endpoint(
    request: PrepareSkillAuthoringRequest,
) -> dict[str, object]:
    try:
        created = _runtime_skill_authoring_service().prepare_brief(
            request.candidate,
            now=int(time.time() * 1000),
        )
        return created.model_dump(mode="json")
    except SkillAuthoringError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500, detail="Failed to prepare skill authoring brief"
        ) from exc


@app.post("/api/skill-authoring/requests/{request_id}/draft")
def submit_skill_draft_endpoint(
    request_id: str, request: SubmitSkillDraftRequest
) -> dict[str, object]:
    try:
        result = _runtime_skill_authoring_service().submit_draft(
            request_id, request.draft_path, now=int(time.time() * 1000)
        )
        return result.model_dump(mode="json")
    except SkillAuthoringError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500, detail="Failed to validate skill draft"
        ) from exc


@app.post("/api/skill-authoring/requests/{request_id}/cancel")
def cancel_skill_authoring_request_endpoint(request_id: str) -> dict[str, object]:
    try:
        return (
            _runtime_skill_authoring_service()
            .cancel(request_id)
            .model_dump(mode="json")
        )
    except SkillAuthoringError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500, detail="Failed to cancel skill authoring request"
        ) from exc


@app.post("/api/skill-authoring/requests/{request_id}/register")
def confirm_skill_registration_endpoint(
    request_id: str,
    request: ConfirmSkillRegistrationRequest,
) -> dict[str, object]:
    try:
        result = _runtime_skill_authoring_service().confirm_registration(
            request_id, confirm=request.confirm, now=int(time.time() * 1000)
        )
        from agent_os.server.runtime import composed_workspace

        composed_workspace.cache_clear()
        return result.model_dump(mode="json")
    except SkillAuthoringError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500, detail="Failed to register skill package"
        ) from exc


@app.post("/api/observations/{observation_id}/outcome")
def record_observation_outcome_endpoint(
    observation_id: str,
    request: RecordObservationOutcomeRequest,
) -> dict[str, object]:
    """Record an explicit operator outcome; automated runs never infer one."""
    try:
        store, workspace_id = _runtime_observation_store()
        observation = store.get(observation_id)
        if observation is None or observation.workspace_id != workspace_id:
            raise HTTPException(status_code=404, detail="Observation not found")
        updated = store.record_outcome(
            observation_id,
            signal=request.signal,  # validated by the store, with one contract source
            evidence=request.evidence,
        )
        if updated is None:
            raise HTTPException(status_code=404, detail="Observation not found")
        return updated.to_dict()
    except ObservationValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=500, detail="Failed to record observation outcome"
        ) from exc


@app.get("/api/observations/assignments/{run_id}")
def get_strategy_assignment_endpoint(run_id: str) -> dict[str, object]:
    """Retrieve strategy assignment audit record for the active workspace."""
    try:
        store, workspace_id = _runtime_observation_store()
        assignment = store.get_strategy_assignment(run_id)
        if assignment is None or assignment.workspace_id != workspace_id:
            raise HTTPException(status_code=404, detail="Strategy assignment not found")
        return assignment.to_dict()
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=500, detail="Failed to read strategy assignment"
        ) from exc


@app.websocket("/api/chat/{thread_id}")
async def websocket_endpoint(websocket: WebSocket, thread_id: str) -> None:
    if _private_api_access_error(websocket) is not None:
        await websocket.close(code=1008, reason="Forbidden")
        return
    requested_protocols = _requested_websocket_protocols(websocket)
    await websocket.accept(
        subprotocol=(
            WEBSOCKET_PROTOCOL if WEBSOCKET_PROTOCOL in requested_protocols else None
        )
    )
    import json

    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
    from langgraph.types import Command

    from agent_os.cli.app import _pending_interrupt
    from agent_os.policy import LocalPolicy, policy_scope

    db_path = os.getenv(CHECKPOINT_DB_ENV, DEFAULT_CHECKPOINT_DB)
    session_key = f"websocket:{thread_id}:{uuid.uuid4()}"
    session_policy = runtime_policy_for_session(session_key)

    try:
        async with AsyncSqliteSaver.from_conn_string(db_path) as saver:
            graph = build_runtime_graph(checkpointer=saver)
            config = runtime_config(thread_id)

            with policy_scope(session_policy):
                try:
                    # A browser can reconnect after the graph has persisted an
                    # interrupt. Restore that pending approval before reading
                    # new user text so the first message resumes it rather
                    # than restarting the task with the same thread id.
                    existing_snapshot = await graph.aget_state(config)
                    existing_prompt = _pending_interrupt(existing_snapshot)
                    pending_interrupt = existing_prompt is not None
                    if existing_prompt is not None:
                        await websocket.send_text(
                            json.dumps(
                                {"type": "interrupt", "prompt": str(existing_prompt)}
                            )
                        )
                    while True:
                        data = await websocket.receive_text()
                        state_update: object = (
                            Command(resume=data)
                            if pending_interrupt
                            else initial_state(data)
                        )

                        async for event in graph.astream_events(
                            state_update, config, version="v2"
                        ):
                            if event["event"] == "on_chat_model_stream":
                                chunk = event["data"]["chunk"]
                                if chunk.content:
                                    await websocket.send_text(
                                        json.dumps(
                                            {"type": "token", "content": chunk.content}
                                        )
                                    )
                        snapshot = await graph.aget_state(config)
                        interrupt_prompt = _pending_interrupt(snapshot)
                        if interrupt_prompt is not None:
                            pending_interrupt = True
                            await websocket.send_text(
                                json.dumps(
                                    {
                                        "type": "interrupt",
                                        "prompt": str(interrupt_prompt),
                                    }
                                )
                            )
                        else:
                            pending_interrupt = False
                            failure = terminal_tool_failure(snapshot)
                            if failure is None:
                                await websocket.send_text(json.dumps({"type": "done"}))
                            else:
                                status, message = failure
                                await websocket.send_text(
                                    json.dumps(
                                        {
                                            "type": "error",
                                            "status": status,
                                            "error": message,
                                        }
                                    )
                                )

                except WebSocketDisconnect:
                    pass
    finally:
        LocalPolicy.clear_session(session_key)
