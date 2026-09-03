
from fastapi.testclient import TestClient

from agent_os.events import emit_run_lifecycle_event
from agent_os.principal import Principal
from agent_os.server.api import app


class InMemoryEventSink:
    name = "in_memory_sink"

    def __init__(self):
        self.events: list[dict] = []

    def emit(self, event: dict):
        self.events.append(event)


def test_emit_all_lifecycle_events_coverage():
    sink = InMemoryEventSink()
    events = [
        ("run.created", "queued"),
        ("run.interrupted", "interrupted"),
        ("run.approved", "running"),
        ("run.completed", "completed"),
        ("run.failed", "failed"),
        ("run.cancelled", "cancelled"),
    ]

    for event_type, status in events:
        emit_run_lifecycle_event(
            event_type,
            run_id="run-coverage",
            workspace_id="ws-cov",
            status=status,
            principal=Principal(id="user:bob", kind="human", display="Bob", on_behalf_of="sec-ops"),
            thread_id="thread-cov",
            sinks=[sink],
        )

    assert len(sink.events) == 6
    emitted_types = [e["event"] for e in sink.events]
    assert emitted_types == [
        "run.created",
        "run.interrupted",
        "run.approved",
        "run.completed",
        "run.failed",
        "run.cancelled",
    ]

    for ev in sink.events:
        assert ev["actor"]["id"] == "user:bob"
        assert ev["actor"]["kind"] == "human"
        assert ev["actor"]["display"] == "Bob"
        assert ev["actor"]["on_behalf_of"] == "sec-ops"
        assert ev["references"]["thread_id"] == "thread-cov"


def test_api_run_lifecycle_emission(monkeypatch):
    client = TestClient(app)
    captured_events = []

    monkeypatch.setattr(
        "agent_os.events.emit_run_lifecycle_event",
        lambda event_type, **kwargs: captured_events.append((event_type, kwargs)),
    )

    monkeypatch.setattr("agent_os.server.api._start_run_task", lambda *args, **kwargs: None)

    # 1. Create run -> emits run.created
    res = client.post(
        "/api/runs",
        json={"task": "Perform security audit", "workspace": "."},
    )
    assert res.status_code == 200
    run_id = res.json()["run_id"]

    assert any(e[0] == "run.created" and e[1]["run_id"] == run_id for e in captured_events)

    # 2. Cancel run -> emits run.cancelled
    res_cancel = client.post(f"/api/runs/{run_id}/cancel")
    assert res_cancel.status_code == 200

    assert any(e[0] == "run.cancelled" and e[1]["run_id"] == run_id for e in captured_events)
