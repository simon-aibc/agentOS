import json
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from agent_os.checkpoints import CHECKPOINT_DB_ENV
from agent_os.runs import append_event, create_run, get_run, list_events, set_status
from agent_os.server.api import EXECUTION_TOKEN_ENV, app, build_graph_data
from agent_os.server.runtime import package_version
from agent_os.update_check import UpdateInfo

client = TestClient(app)


def test_health_version():
    resp = client.get("/api/health")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["status"] == "ok"
    assert payload["version"] == package_version()
    assert "current_version" in payload
    assert "latest_version" in payload
    assert "update_available" in payload
    assert payload["workspace"]["status"] == "not_configured"
    assert payload["active_runs"] == 0


def test_health_never_fetches_network_inline(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_OS_UPDATE_CACHE", str(tmp_path / "nonexistent.json"))
    with patch("urllib.request.urlopen") as mock_urlopen:
        resp = client.get("/api/health")
        assert resp.status_code == 200
        assert mock_urlopen.call_count == 0  # Zero network calls on health poll!


def test_update_check_api_endpoint(monkeypatch):
    mock_info = UpdateInfo(
        current_version="2.2.0",
        latest_version="2.3.0",
        update_available=True,
        release_url="https://github.com/simon-aibc/agent-os-langgraph/releases/tag/v2.3.0",
    )
    monkeypatch.setattr(
        "agent_os.server.api.check_for_update",
        lambda *args, **kwargs: mock_info,
    )
    resp = client.post("/api/update/check")
    assert resp.status_code == 200
    data = resp.json()
    assert "current_version" in data
    assert "latest_version" in data
    assert "update_available" in data
    assert data["current_version"] == "2.2.0"
    assert data["latest_version"] == "2.3.0"
    assert data["update_available"] is True
    assert (
        data["release_url"]
        == "https://github.com/simon-aibc/agent-os-langgraph/releases/tag/v2.3.0"
    )


def test_health_reports_configured_workspace(tmp_path, monkeypatch):
    workspace_path = tmp_path / "workspace.toml"
    (tmp_path / "SYSTEM.md").write_text("# System\nPrivate context.\n")
    workspace_path.write_text(
        "\n".join(
            [
                "skills = []",
                'connectors = ["memory"]',
                "",
                "[workspace]",
                'name = "private-test"',
                "",
                "[backends]",
                'architect = "codex"',
                'executor = "codex"',
                "",
                "[memory]",
                'type = "markdown"',
                'path = "."',
                "",
                "[context]",
                'sources = ["SYSTEM.md"]',
                "max_age_days = 3650",
                "",
            ]
        )
    )
    monkeypatch.setenv("AGENT_OS_WORKSPACE", str(workspace_path))
    from agent_os.server import runtime

    runtime.composed_workspace.cache_clear()

    try:
        resp = client.get("/api/health")
        payload = resp.json()
        assert resp.status_code == 200
        assert payload["workspace"]["status"] == "ok"
        assert payload["workspace"]["name"] == "private-test"
        assert isinstance(payload["workspace"]["skills"], int)
        assert payload["workspace"]["hot_context"] is True
    finally:
        runtime.composed_workspace.cache_clear()


def test_cors_allows_console_origin():
    origin = "http://127.0.0.1:4100"
    resp = client.get("/api/health", headers={"Origin": origin})
    assert resp.status_code == 200
    assert resp.headers.get("access-control-allow-origin") == origin


def test_private_api_rejects_an_untrusted_browser_origin():
    response = client.get(
        "/api/runs",
        headers={"Origin": "https://untrusted.example"},
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Origin is not allowed"


def test_private_api_requires_execution_token_when_configured(monkeypatch, tmp_path):
    monkeypatch.setenv(EXECUTION_TOKEN_ENV, "execution-secret")
    monkeypatch.setenv(CHECKPOINT_DB_ENV, str(tmp_path / "checkpoints.sqlite"))

    blocked = client.get("/api/runs")
    assert blocked.status_code == 403

    allowed = client.get(
        "/api/runs",
        headers={"X-Execution-Token": "execution-secret"},
    )
    assert allowed.status_code == 200


def test_run_api_rejects_invalid_strategy_override():
    response = client.post(
        "/api/runs",
        json={"task": "ship it", "strategy_id": "untrusted-v1"},
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "Strategy is not allowed for this task kind"


def test_run_api_accepts_opaque_task_signature_and_rejects_invalid_values(
    tmp_path, monkeypatch
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("AGENT_OS_SANDBOX", str(tmp_path))
    signature = "sha256:v1:" + "a" * 64
    created = client.post(
        "/api/runs",
        json={
            "task": "run a report",
            "workspace": "workspace",
            "task_signature": signature,
        },
    )
    assert created.status_code == 200
    run = client.get(f"/api/runs/{created.json()['run_id']}").json()
    assert run["task_signature"] == signature

    invalid = client.post(
        "/api/runs",
        json={"task": "run a report", "workspace": "workspace", "task_signature": "report"},
    )
    assert invalid.status_code == 422
    assert invalid.json()["detail"] == "task_signature must be a sha256:v1 digest"


def test_api_sessions_list(tmp_path, monkeypatch):
    db_path = str(tmp_path / "checkpoints.sqlite")
    monkeypatch.setenv("CHECKPOINT_DB_ENV", db_path)

    # Insert a session
    from agent_os.sessions import upsert_session

    upsert_session("thr1", "Test session")

    resp = client.get("/api/sessions")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["thread_id"] == "thr1"
    assert data[0]["title"] == "Test session"
    assert "created_at" in data[0]


def test_api_brief(monkeypatch):
    from agent_os.brief_runtime import BriefExecutionResult

    def mock_execute_brief(*args, **kwargs):
        return BriefExecutionResult(
            date="2026-01-01", content="Mock brief content", ref=None
        )

    monkeypatch.setattr("agent_os.brief_runtime.execute_brief", mock_execute_brief)

    resp = client.post("/api/brief")
    assert resp.status_code == 200
    data = resp.json()
    assert "date" in data
    assert data["content"] == "Mock brief content"


def test_api_graph_shape():
    resp = client.get("/api/graph")
    assert resp.status_code == 200
    data = resp.json()
    assert "nodes" in data
    assert "edges" in data
    assert isinstance(data["nodes"], list)
    assert isinstance(data["edges"], list)


class FakeMemoryConnector:
    @property
    def name(self):
        return "fake"

    def __init__(self):
        self.notes = [
            {"ref": "team/alpha", "title": "Alpha"},
            {"ref": "beta", "title": "Beta Page"},
            {"ref": "gamma", "title": None},
            {"ref": "", "title": "No Ref"},
        ]
        self.reads = {
            "team/alpha": {
                "ref": "team/alpha",
                "content": "",
                "frontmatter": {},
                "links": [
                    "beta",
                    "Beta Page",
                    "Alpha",
                    "missing",
                    "team/alpha",
                ],
            },
            "beta": {
                "ref": "beta",
                "content": "",
                "frontmatter": {},
                "links": ["gamma"],
            },
            "gamma": {
                "ref": "gamma",
                "content": "",
                "frontmatter": {},
                "links": [],
            },
        }

    def search(self, query, limit=10):
        return []

    def list_notes(self, filters=None):
        return self.notes

    def read_note(self, slug_or_path):
        return self.reads[slug_or_path]


def test_build_graph_data_nodes_and_edges():
    data = build_graph_data(FakeMemoryConnector(), 10)

    assert data["nodes"] == [
        {
            "id": "team/alpha",
            "title": "Alpha",
            "group": "team",
            "type": "page",
        },
        {"id": "beta", "title": "Beta Page", "group": "", "type": "page"},
        {"id": "gamma", "title": "gamma", "group": "", "type": "page"},
    ]
    assert data["edges"] == [
        {"source": "team/alpha", "target": "beta", "type": "link"},
        {"source": "beta", "target": "gamma", "type": "link"},
    ]


def test_build_graph_data_respects_node_cap():
    data = build_graph_data(FakeMemoryConnector(), 2)

    assert [node["id"] for node in data["nodes"]] == ["team/alpha", "beta"]
    assert data["edges"] == [{"source": "team/alpha", "target": "beta", "type": "link"}]


def test_api_graph_empty_state_fallback(monkeypatch):
    def raise_memory_connector():
        raise RuntimeError("gbrain unavailable")

    monkeypatch.setattr("agent_os.server.api.memory_connector", raise_memory_connector)

    resp = client.get("/api/graph")

    assert resp.status_code == 200
    assert resp.json() == {"nodes": [], "edges": []}


def test_run_events_sse_replays_from_offset_and_ends(tmp_path, monkeypatch):
    db_path = str(tmp_path / "checkpoints.sqlite")
    monkeypatch.setenv(CHECKPOINT_DB_ENV, db_path)
    run_id = create_run("thread-sse", "workspace", "task")
    append_event(run_id, "node", {"name": "planner", "event": "on_chain_start"})
    append_event(run_id, "token", {"content": "hello"})
    append_event(run_id, "result", {})
    set_status(run_id, "completed", ended=True)

    resp = client.get(f"/api/runs/{run_id}/events?after=1")

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    frames = [frame for frame in resp.text.strip().split("\n\n") if frame]
    replayed = [json.loads(frame.removeprefix("data: ")) for frame in frames[:2]]
    assert [event["seq"] for event in replayed] == [2, 3]
    assert [event["kind"] for event in replayed] == ["token", "result"]
    assert frames[-1] == "event: end\ndata: {}"


def test_run_api_lifecycle_create_interrupt_approve_complete(tmp_path, monkeypatch):
    db_path = str(tmp_path / "checkpoints.sqlite")
    monkeypatch.setenv(CHECKPOINT_DB_ENV, db_path)
    workspace = tmp_path / "workspace-a"
    workspace.mkdir()
    monkeypatch.setenv("AGENT_OS_SANDBOX", str(tmp_path))
    calls = []

    async def fake_execute_run(run_id, thread_id, task, *, resume_feedback=None):
        calls.append(
            {
                "run_id": run_id,
                "thread_id": thread_id,
                "task": task,
                "resume_feedback": resume_feedback,
            }
        )
        if resume_feedback is None:
            append_event(run_id, "interrupt", {"prompt": "First prompt"})
            append_event(run_id, "interrupt", {"prompt": "Approve revised plan?"})
            set_status(run_id, "interrupted")
        else:
            append_event(run_id, "result", {})
            set_status(run_id, "completed", ended=True)

    monkeypatch.setattr("agent_os.server.api.execute_run", fake_execute_run)

    create_resp = client.post(
        "/api/runs",
        json={"task": "ship it", "workspace": "workspace-a"},
    )

    assert create_resp.status_code == 200
    created = create_resp.json()
    assert created["status"] == "queued"
    assert created["thread_id"]

    interrupted_resp = client.get(f"/api/runs/{created['run_id']}")
    assert interrupted_resp.status_code == 200
    interrupted = interrupted_resp.json()
    assert interrupted["status"] == "interrupted"
    assert interrupted["workspace"] == str(workspace.resolve())
    assert interrupted["interrupt"] == "Approve revised plan?"

    approve_resp = client.post(
        f"/api/runs/{created['run_id']}/approve",
        json={"feedback": "approved"},
    )

    assert approve_resp.status_code == 200
    assert approve_resp.json() == {"run_id": created["run_id"], "status": "running"}

    completed_resp = client.get(f"/api/runs/{created['run_id']}")
    assert completed_resp.status_code == 200
    completed = completed_resp.json()
    assert completed["status"] == "completed"
    assert completed["interrupt"] is None
    assert calls == [
        {
            "run_id": created["run_id"],
            "thread_id": created["thread_id"],
            "task": "ship it",
            "resume_feedback": None,
        },
        {
            "run_id": created["run_id"],
            "thread_id": created["thread_id"],
            "task": "",
            "resume_feedback": "approved",
        },
    ]


def test_run_api_rejects_workspace_outside_sandbox(tmp_path, monkeypatch):
    db_path = str(tmp_path / "checkpoints.sqlite")
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    monkeypatch.setenv(CHECKPOINT_DB_ENV, db_path)
    monkeypatch.setenv("AGENT_OS_SANDBOX", str(sandbox))

    response = client.post(
        "/api/runs",
        json={"task": "unsafe", "workspace": str(tmp_path)},
    )

    assert response.status_code == 422
    assert "outside the sandbox" in response.json()["detail"]


def test_run_api_cancel_path(tmp_path, monkeypatch):
    from agent_os.policy import LocalPolicy, derive_permission_key
    from agent_os.schemas import ActionProposal

    db_path = str(tmp_path / "checkpoints.sqlite")
    monkeypatch.setenv(CHECKPOINT_DB_ENV, db_path)
    run_id = create_run("thread-cancel", "workspace-a", "task")
    set_status(run_id, "interrupted")
    action = ActionProposal(
        tool="memory.write",
        connector="markdown_vault",
        arguments={"ref": "AI/Decisions/cancel.md", "mode": "create"},
        side_effect="write",
    )
    permission_key = derive_permission_key(action)
    assert permission_key is not None
    LocalPolicy.approve_session(run_id, permission_key)

    resp = client.post(f"/api/runs/{run_id}/cancel")

    assert resp.status_code == 200
    assert resp.json() == {"run_id": run_id, "status": "cancelled"}
    run = get_run(run_id)
    assert run["status"] == "cancelled"
    assert run["ended_at"] is not None
    events = list_events(run_id)
    assert events[-1]["kind"] == "status"
    assert events[-1]["payload"] == {"status": "cancelled"}
    assert (
        LocalPolicy(session_key=run_id).evaluate(action).decision == "require_approval"
    )


def test_run_api_404s(tmp_path, monkeypatch):
    db_path = str(tmp_path / "checkpoints.sqlite")
    monkeypatch.setenv(CHECKPOINT_DB_ENV, db_path)

    assert client.get("/api/runs/missing").status_code == 404
    assert client.post("/api/runs/missing/approve", json={}).status_code == 404
    assert client.post("/api/runs/missing/cancel").status_code == 404


def test_completed_run_api_surfaces_result_self_check(tmp_path, monkeypatch):
    db_path = str(tmp_path / "checkpoints.sqlite")
    monkeypatch.setenv(CHECKPOINT_DB_ENV, db_path)
    run_id = create_run("thread-self-check", None, "validate")
    self_check = {
        "total": 3,
        "met": 2,
        "results": [{"rule_id": "one", "passed": True}],
    }
    append_event(run_id, "result", {"self_check": self_check})
    set_status(run_id, "completed", ended=True)

    detail = client.get(f"/api/runs/{run_id}")
    listing = client.get("/api/runs")

    assert detail.status_code == listing.status_code == 200
    assert detail.json()["self_check"] == self_check
    assert (
        next(item for item in listing.json() if item["run_id"] == run_id)["self_check"]
        == self_check
    )


def test_run_api_approve_conflict_when_not_interrupted(tmp_path, monkeypatch):
    db_path = str(tmp_path / "checkpoints.sqlite")
    monkeypatch.setenv(CHECKPOINT_DB_ENV, db_path)
    run_id = create_run("thread-approve-conflict", None, "task")

    resp = client.post(f"/api/runs/{run_id}/approve", json={})

    assert resp.status_code == 409


def test_run_api_cancel_conflict_when_terminal(tmp_path, monkeypatch):
    db_path = str(tmp_path / "checkpoints.sqlite")
    monkeypatch.setenv(CHECKPOINT_DB_ENV, db_path)
    run_id = create_run("thread-cancel-conflict", None, "task")
    set_status(run_id, "completed", ended=True)

    resp = client.post(f"/api/runs/{run_id}/cancel")

    assert resp.status_code == 409


def test_run_api_list_filters(tmp_path, monkeypatch):
    db_path = str(tmp_path / "checkpoints.sqlite")
    monkeypatch.setenv(CHECKPOINT_DB_ENV, db_path)
    queued = create_run("thread-queued", "workspace-a", "task")
    running_a = create_run("thread-running-a", "workspace-a", "task")
    running_b = create_run("thread-running-b", "workspace-b", "task")
    set_status(running_a, "running")
    set_status(running_b, "running")

    queued_resp = client.get("/api/runs?status=queued")
    assert queued_resp.status_code == 200
    assert [run["run_id"] for run in queued_resp.json()] == [queued]

    workspace_resp = client.get("/api/runs?workspace=workspace-a")
    assert workspace_resp.status_code == 200
    assert {run["run_id"] for run in workspace_resp.json()} == {queued, running_a}

    filtered_resp = client.get("/api/runs?status=running&workspace=workspace-a")
    assert filtered_resp.status_code == 200
    assert [run["run_id"] for run in filtered_resp.json()] == [running_a]

    limit_resp = client.get("/api/runs?limit=2")
    assert limit_resp.status_code == 200
    assert len(limit_resp.json()) == 2


def test_run_api_approvals_endpoint(tmp_path, monkeypatch):
    db_path = str(tmp_path / "checkpoints.sqlite")
    monkeypatch.setenv(CHECKPOINT_DB_ENV, db_path)
    monkeypatch.setenv(EXECUTION_TOKEN_ENV, "secret-token")

    # 404 for unknown run
    assert client.get("/api/runs/nonexistent-run/approvals", headers={"x-execution-token": "secret-token"}).status_code == 404

    # Empty list for run with no approvals
    run_id = create_run("thread-audit", None, "task")
    resp = client.get(f"/api/runs/{run_id}/approvals", headers={"x-execution-token": "secret-token"})
    assert resp.status_code == 200
    assert resp.json() == []

    # Transition to interrupted
    set_status(run_id, "interrupted")

    # Approve with feedback and attempt to spoof headers
    approve_resp = client.post(
        f"/api/runs/{run_id}/approve",
        json={"feedback": "LGTM from review"},
        headers={
            "x-actor-label": "spoofed-reviewer",
            "x-on-behalf-of": "spoofed-vip",
            "x-execution-token": "secret-token",
        },
    )
    assert approve_resp.status_code == 200

    # Get approvals history - verify spoofed headers were ignored
    approvals_resp = client.get(f"/api/runs/{run_id}/approvals", headers={"x-execution-token": "secret-token"})
    assert approvals_resp.status_code == 200
    history = approvals_resp.json()
    assert len(history) == 1
    assert history[0]["seq"] == 1
    assert history[0]["decision"] == "approved"
    assert history[0]["actor_id"] == "token:execution"
    assert history[0]["actor_kind"] == "service"
    assert history[0]["on_behalf_of"] is None
    assert history[0]["reason"] == "LGTM from review"

    # Create another run to test atomic cancel audit
    cancel_run_id = create_run("thread-cancel-audit", None, "cancel task")
    set_status(cancel_run_id, "interrupted")

    cancel_resp = client.post(
        f"/api/runs/{cancel_run_id}/cancel",
        headers={
            "x-actor-label": "spoofed-ops",
            "x-on-behalf-of": "spoofed-ceo",
            "x-execution-token": "secret-token",
        },
    )
    assert cancel_resp.status_code == 200

    # Verify decision recorded in approvals history and status changed to cancelled
    assert get_run(cancel_run_id)["status"] == "cancelled"
    cancel_history = client.get(f"/api/runs/{cancel_run_id}/approvals", headers={"x-execution-token": "secret-token"}).json()
    assert len(cancel_history) == 1
    assert cancel_history[0]["seq"] == 1
    assert cancel_history[0]["decision"] == "cancelled"
    assert cancel_history[0]["actor_id"] == "token:execution"
    assert cancel_history[0]["on_behalf_of"] is None

    # Attempting to cancel already-cancelled run -> 409 Conflict, no duplicate audit row
    cancel_again = client.post(f"/api/runs/{cancel_run_id}/cancel", headers={"x-execution-token": "secret-token"})
    assert cancel_again.status_code == 409
    cancel_history_after = client.get(f"/api/runs/{cancel_run_id}/approvals", headers={"x-execution-token": "secret-token"}).json()
    assert len(cancel_history_after) == 1


def test_run_api_create_records_creator_identity(tmp_path, monkeypatch):
    db_path = str(tmp_path / "checkpoints.sqlite")
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    ws = sandbox / "workspace-creator"
    ws.mkdir()
    monkeypatch.setenv(CHECKPOINT_DB_ENV, db_path)
    monkeypatch.setenv("AGENT_OS_SANDBOX", str(sandbox))
    monkeypatch.setenv(EXECUTION_TOKEN_ENV, "secret-token")

    # 1. Authenticated via token -> token:execution
    resp = client.post(
        "/api/runs",
        json={"task": "create with principal", "workspace": "workspace-creator"},
        headers={
            "x-actor-label": "spoofed-deploy-bot",
            "x-on-behalf-of": "spoofed-ci",
            "x-execution-token": "secret-token",
        },
    )
    assert resp.status_code == 200
    run_id = resp.json()["run_id"]

    run = get_run(run_id)
    assert run is not None
    assert run["created_by"] == "token:execution"
    assert run["created_by_kind"] == "service"
    assert run["workspace_id"] == str(ws.resolve())

    # 2. Unauthenticated loopback without token requirement -> local:<user>
    monkeypatch.delenv(EXECUTION_TOKEN_ENV, raising=False)
    resp_loopback = client.post(
        "/api/runs",
        json={"task": "create loopback", "workspace": "workspace-creator"},
        headers={
            "x-actor-label": "spoofed-deploy-bot",
            "x-on-behalf-of": "spoofed-ci",
        },
    )
    assert resp_loopback.status_code == 200
    run_loopback_id = resp_loopback.json()["run_id"]
    run_loopback = get_run(run_loopback_id)
    assert run_loopback is not None
    assert run_loopback["created_by"].startswith("local:")
    assert run_loopback["created_by_kind"] == "human"


@pytest.mark.anyio
async def test_ws_chat_streams(monkeypatch, tmp_path):
    db_path = str(tmp_path / "checkpoints.sqlite")
    monkeypatch.setenv("CHECKPOINT_DB_ENV", db_path)
    monkeypatch.setenv("AGENT_OS_PERMISSIONS_DB", str(tmp_path / "permissions.db"))

    # Mock graph building and running
    class MockGraph:
        async def astream_events(self, state_update, config, version):
            from langchain_core.messages import AIMessageChunk

            yield {
                "event": "on_chat_model_stream",
                "data": {"chunk": AIMessageChunk(content="Hel")},
            }
            yield {
                "event": "on_chat_model_stream",
                "data": {"chunk": AIMessageChunk(content="lo")},
            }

        async def aget_state(self, config):
            from types import SimpleNamespace

            return SimpleNamespace(tasks=())

    def mock_build_graph(checkpointer=None):
        return MockGraph()

    monkeypatch.setattr("agent_os.graph.build_graph", mock_build_graph)

    with client.websocket_connect("/api/chat/thr_ws") as websocket:
        websocket.send_text("Hi there")

        # Should receive stream tokens then done
        msg1 = websocket.receive_json()
        assert msg1["type"] == "token"
        assert msg1["content"] == "Hel"

        msg2 = websocket.receive_json()
        assert msg2["type"] == "token"
        assert msg2["content"] == "lo"

        msg3 = websocket.receive_json()
        assert msg3["type"] == "done"


def test_ws_chat_rejects_an_untrusted_browser_origin():
    with pytest.raises(WebSocketDisconnect) as error:
        with client.websocket_connect(
            "/api/chat/untrusted-origin",
            headers={"Origin": "https://untrusted.example"},
        ):
            pass

    assert error.value.code == 1008


def test_ws_chat_requires_execution_token_when_configured(monkeypatch, tmp_path):
    from types import SimpleNamespace

    monkeypatch.setenv(EXECUTION_TOKEN_ENV, "execution-secret")
    monkeypatch.setenv(CHECKPOINT_DB_ENV, str(tmp_path / "checkpoints.sqlite"))

    class IdleGraph:
        async def astream_events(self, state_update, config, version):
            if False:
                yield {}

        async def aget_state(self, config):
            return SimpleNamespace(tasks=(), values={})

    monkeypatch.setattr(
        "agent_os.server.api.build_runtime_graph",
        lambda *, checkpointer: IdleGraph(),
    )

    with pytest.raises(WebSocketDisconnect) as error:
        with client.websocket_connect("/api/chat/missing-token"):
            pass
    assert error.value.code == 1008

    with client.websocket_connect(
        "/api/chat/valid-token",
        headers={"X-Execution-Token": "execution-secret"},
    ):
        pass

    with client.websocket_connect(
        "/api/chat/browser-token",
        subprotocols=["agent-os", "agent-os-token.execution-secret"],
    ) as websocket:
        assert websocket.accepted_subprotocol == "agent-os"


@pytest.mark.anyio
async def test_ws_chat_binds_standalone_policy_to_nested_memory_write(
    monkeypatch, tmp_path
):
    from agent_os.connectors import MarkdownVaultConnector
    from agent_os.memory_gate import gated_write
    from agent_os.permission_store import PermissionRule, SqlitePermissionStore
    from agent_os.policy import derive_permission_key
    from agent_os.schemas import ActionProposal, MemoryWriteProposal

    monkeypatch.setenv(CHECKPOINT_DB_ENV, str(tmp_path / "checkpoints.sqlite"))
    permission_db = tmp_path / "permissions.db"
    monkeypatch.setenv("AGENT_OS_PERMISSIONS_DB", str(permission_db))
    connector = MarkdownVaultConnector(str(tmp_path / "vault"))
    proposal = MemoryWriteProposal(
        connector="markdown_vault",
        ref="AI/Decisions/websocket.md",
        mode="create",
        content_preview="websocket policy wiring",
        side_effect="write",
    )
    key = derive_permission_key(
        ActionProposal(
            tool="memory.write",
            connector="markdown_vault",
            arguments={"ref": proposal.ref, "mode": proposal.mode},
            side_effect="write",
        )
    )
    assert key is not None
    store = SqlitePermissionStore(str(permission_db))
    store.upsert(
        PermissionRule(
            permission_key=key,
            effect="always_approve",
            tier_at_creation="low",
        )
    )

    class PolicyGraph:
        async def astream_events(self, state_update, config, version):
            result = gated_write(connector, proposal, "websocket content")
            assert result.committed is True
            if False:
                yield {}

        async def aget_state(self, config):
            from types import SimpleNamespace

            return SimpleNamespace(tasks=())

    monkeypatch.setattr(
        "agent_os.server.api.build_runtime_graph", lambda *, checkpointer: PolicyGraph()
    )
    monkeypatch.setattr(
        "agent_os.server.api.initial_state", lambda data: {"task": data}
    )

    with client.websocket_connect("/api/chat/policy-thread") as websocket:
        websocket.send_text("write")
        assert websocket.receive_json() == {"type": "done"}

    assert (tmp_path / "vault" / proposal.ref).exists()


@pytest.mark.anyio
async def test_ws_chat_reports_unsuccessful_native_tool_result(monkeypatch, tmp_path):
    """A rejected policy write must not be rendered as a successful chat turn."""
    from types import SimpleNamespace

    from agent_os.schemas import ToolExecutionResult

    monkeypatch.setenv(CHECKPOINT_DB_ENV, str(tmp_path / "checkpoints.sqlite"))
    monkeypatch.setenv("AGENT_OS_PERMISSIONS_DB", str(tmp_path / "permissions.db"))

    class RejectedPolicyGraph:
        async def astream_events(self, state_update, config, version):
            if False:
                yield {}

        async def aget_state(self, config):
            return SimpleNamespace(
                tasks=(),
                values={
                    "tool_result": ToolExecutionResult(
                        tool="memory_write",
                        output=(
                            '{"status": "cancelled", '
                            '"errors": ["User rejected memory write"]}'
                        ),
                        success=False,
                    )
                },
            )

    monkeypatch.setattr(
        "agent_os.server.api.build_runtime_graph",
        lambda *, checkpointer: RejectedPolicyGraph(),
    )
    monkeypatch.setattr(
        "agent_os.server.api.initial_state", lambda data: {"task": data}
    )

    with client.websocket_connect("/api/chat/ws-rejected-write") as websocket:
        websocket.send_text("write memory")
        assert websocket.receive_json() == {
            "type": "error",
            "status": "cancelled",
            "error": "User rejected memory write",
        }


@pytest.mark.anyio
async def test_ws_chat_resumes_policy_learning_and_reapplies_rule(
    monkeypatch, tmp_path
):
    """WebSocket clients can reconnect, teach a rule, then reuse it."""
    from langgraph.graph import END, START, StateGraph
    from pydantic import BaseModel

    from agent_os.connectors import MarkdownVaultConnector
    from agent_os.memory_gate import gated_write
    from agent_os.schemas import MemoryWriteProposal

    monkeypatch.setenv(CHECKPOINT_DB_ENV, str(tmp_path / "checkpoints.sqlite"))
    monkeypatch.setenv("AGENT_OS_PERMISSIONS_DB", str(tmp_path / "permissions.db"))
    connector = MarkdownVaultConnector(str(tmp_path / "vault"))
    proposal = MemoryWriteProposal(
        connector="markdown_vault",
        ref="AI/Decisions/ws-learning.md",
        mode="append",
        content_preview="websocket learning",
        side_effect="write",
    )

    class WebsocketState(BaseModel):
        task: str
        committed: bool = False

    def build_policy_graph(*, checkpointer):
        builder = StateGraph(WebsocketState)

        def write_node(state: WebsocketState) -> dict[str, object]:
            result = gated_write(connector, proposal, "entry")
            return {"committed": result.committed}

        builder.add_node("write", write_node)
        builder.add_edge(START, "write")
        builder.add_edge("write", END)
        return builder.compile(checkpointer=checkpointer)

    monkeypatch.setattr("agent_os.server.api.build_runtime_graph", build_policy_graph)
    monkeypatch.setattr(
        "agent_os.server.api.initial_state", lambda data: {"task": data}
    )

    with client.websocket_connect("/api/chat/ws-learning-first") as websocket:
        websocket.send_text("write memory")
        interrupt = websocket.receive_json()
        assert interrupt["type"] == "interrupt"
        assert interrupt["prompt"].startswith("Policy approval required:")

    # A new socket with the same thread restores the persisted policy
    # interrupt, rather than treating the first reply as a brand-new task.
    with client.websocket_connect("/api/chat/ws-learning-first") as websocket:
        interrupt = websocket.receive_json()
        assert interrupt["type"] == "interrupt"
        assert interrupt["prompt"].startswith("Policy approval required:")

        websocket.send_text("always_approve")
        assert websocket.receive_json() == {"type": "done"}

    with client.websocket_connect("/api/chat/ws-learning-second") as websocket:
        websocket.send_text("write memory again")
        assert websocket.receive_json() == {"type": "done"}

    assert (tmp_path / "vault" / proposal.ref).read_text(
        encoding="utf-8"
    ) == "entry\nentry"


def test_serve_missing_extra(monkeypatch, capsys):
    import asyncio

    # Hide fastapi temporarily
    import sys

    from agent_os.cli.app import async_main

    monkeypatch.setitem(sys.modules, "fastapi", None)
    monkeypatch.setitem(sys.modules, "uvicorn", None)

    exit_code = asyncio.run(async_main(["serve"]))
    assert exit_code == 1

    captured = capsys.readouterr()
    assert "FastAPI dependencies not installed" in captured.out
    assert "pip install agent-os-langgraph[serve]" in captured.out


@pytest.mark.anyio
async def test_serve_refuses_remote_bind_without_execution_token(monkeypatch, capsys):
    from agent_os.cli.app import async_main

    monkeypatch.delenv(EXECUTION_TOKEN_ENV, raising=False)

    exit_code = await async_main(["serve", "--host", "0.0.0.0"])

    assert exit_code == 2
    assert "AGENT_OS_EXECUTION_TOKEN" in capsys.readouterr().out


def test_bind_localhost_default():
    from agent_os.cli.app import build_parser

    parser = build_parser()
    args = parser.parse_args(["serve"])
    assert args.host == "127.0.0.1"
    assert args.port == 4680


def test_get_strategy_assignment_api_and_workspace_scoping(tmp_path, monkeypatch):
    from agent_os.observations import SqliteObservationStore
    from agent_os.server import runtime

    database = tmp_path / "observations.db"
    monkeypatch.setenv("AGENT_OS_OBSERVATIONS_DB", str(database))
    monkeypatch.setattr(runtime, "composed_workspace", lambda: None)
    store = SqliteObservationStore(str(database))

    summary = {
        "labelled_counts": {"default-v1": 7, "verification-first-v1": 6},
        "scores": {"default-v1": 0.43, "verification-first-v1": 0.83},
        "winner": "verification-first-v1",
        "threshold_met": True,
        "margin": 0.40,
    }
    store.assign_strategy(
        workspace_id="standalone",
        task_kind="workflow",
        run_id="audit-run-123",
        strategy_id="verification-first-v1",
        strategy_version=1,
        selection_reason="evidence_backed",
        selector_version="v1.0",
        evidence_summary=summary,
    )
    store.assign_strategy(
        workspace_id="other-workspace",
        task_kind="workflow",
        run_id="other-run-456",
        strategy_id="concise-plan-v1",
        strategy_version=1,
        selection_reason="explicit",
        selector_version="v1.0",
        evidence_summary=None,
    )

    # 404 on unknown run_id
    resp_404 = client.get("/api/observations/assignments/nonexistent")
    assert resp_404.status_code == 404
    assert resp_404.json()["detail"] == "Strategy assignment not found"

    # 404 on cross-workspace access
    resp_other = client.get("/api/observations/assignments/other-run-456")
    assert resp_other.status_code == 404

    # 200 on matching workspace
    resp_ok = client.get("/api/observations/assignments/audit-run-123")
    assert resp_ok.status_code == 200
    data = resp_ok.json()
    assert data["run_id"] == "audit-run-123"
    assert data["workspace_id"] == "standalone"
    assert data["strategy_id"] == "verification-first-v1"
    assert data["strategy_version"] == 1
    assert data["selection_reason"] == "evidence_backed"
    assert data["selector_version"] == "v1.0"
    assert data["evidence_summary"] == summary
    assert "selected_at" in data

    del store
    import gc

    gc.collect()
