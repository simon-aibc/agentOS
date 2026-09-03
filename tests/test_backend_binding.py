import io
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.types import Command
from rich.console import Console

from agent_os.backends import AuthStatus, BackendRegistry
from agent_os.bindings import (
    binding_conflicts,
    resolve_backend_binding,
    validate_backend_binding,
)
from agent_os.checkpoints import get_checkpoint_serializer
from agent_os.cli.app import async_main
from agent_os.graph import build_graph
from agent_os.routing import build_runtime_config
from agent_os.schemas import ArchitectBrief, ExecutorReport
from agent_os.state import BackendBinding


def test_backend_binding_normalizes_sandbox_root(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)

    binding = BackendBinding(
        router=None,
        architect=None,
        executor=None,
        profile_name=None,
        sandbox_root="./sandbox",
    )

    assert binding.sandbox_root == str((tmp_path / "sandbox").resolve())
    assert Path(binding.sandbox_root).is_absolute()


def test_backend_binding_round_trips_checkpoint_serializer(tmp_path):
    binding = BackendBinding(
        router="ollama/router",
        architect="cli/claude-code",
        executor="cli/codex",
        profile_name="local",
        sandbox_root=str(tmp_path),
    )
    serializer = get_checkpoint_serializer()

    restored = serializer.loads_typed(serializer.dumps_typed(binding))

    assert restored == binding
    assert isinstance(restored, BackendBinding)


def test_resolve_backend_binding_reads_effective_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_ROUTER", "ollama/router")
    monkeypatch.setenv("LLM_ARCHITECT", "cli/claude-code")
    monkeypatch.setenv("LLM_EXECUTOR", "cli/codex")
    monkeypatch.setenv("AGENT_OS_SANDBOX", str(tmp_path / "box"))

    binding = resolve_backend_binding("subscription")

    assert binding.router == "ollama/router"
    assert binding.architect == "cli/claude-code"
    assert binding.executor == "cli/codex"
    assert binding.profile_name == "subscription"
    assert binding.sandbox_root == str((tmp_path / "box").resolve())


def test_binding_conflicts_ignore_profile_name_and_normalized_path(tmp_path):
    first = BackendBinding(
        router="router",
        architect="architect",
        executor="executor",
        profile_name="first",
        sandbox_root=str(tmp_path),
    )
    second = first.model_copy(
        update={"profile_name": "second", "sandbox_root": str(tmp_path / ".")}
    )

    assert binding_conflicts(first, BackendBinding.model_validate(second)) == {}


def test_validate_binding_rejects_unknown_cli_adapter(tmp_path):
    binding = BackendBinding(
        router=None,
        architect="cli/missing",
        executor=None,
        profile_name=None,
        sandbox_root=str(tmp_path),
    )

    with pytest.raises(ValueError, match="Unknown backend adapter 'missing'"):
        validate_backend_binding(binding, BackendRegistry())


def make_console():
    output = io.StringIO()
    return Console(file=output, force_terminal=False, color_system=None), output


class ResumeBindingGraph:
    def __init__(self, binding, *, node="architect"):
        self.binding = binding
        self.node = node
        self.stage = "paused"
        self.update_calls = []
        self.stream_inputs = []

    async def aget_state(self, config):
        if self.stage == "done":
            return SimpleNamespace(
                created_at="now",
                values={"backend_binding": self.binding},
                next=(),
                tasks=(),
            )
        return SimpleNamespace(
            created_at="now",
            values={"task": "resume", "backend_binding": self.binding},
            next=(self.node,),
            tasks=(),
        )

    async def aupdate_state(self, config, values):
        self.update_calls.append(values)
        self.binding = values["backend_binding"]
        return config

    async def astream_events(self, graph_input, config, version):
        self.stream_inputs.append(graph_input)
        self.stage = "done"
        yield {"event": "unknown"}


class FreshBindingGraph:
    def __init__(self):
        self.input_state = None

    async def astream_events(self, graph_input, config, version):
        self.input_state = graph_input
        yield {"event": "unknown"}

    async def aget_state(self, config):
        return SimpleNamespace(
            created_at="now",
            values=self.input_state,
            next=(),
            tasks=(),
        )


@pytest.mark.anyio
async def test_fresh_workflow_persists_current_binding(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_OS_SANDBOX", str(tmp_path / "sandbox"))
    graph = FreshBindingGraph()

    code = await async_main(
        ["task", "--thread-id", "fresh-binding"],
        graph_factory=lambda: graph,
    )

    assert code == 0
    binding = graph.input_state["backend_binding"]
    assert isinstance(binding, BackendBinding)
    assert binding.sandbox_root == str((tmp_path / "sandbox").resolve())


@pytest.mark.anyio
async def test_resume_identical_binding_has_no_warning(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_OS_SANDBOX", str(tmp_path))
    persisted = resolve_backend_binding("old-profile")
    current = persisted.model_copy(update={"profile_name": "new-profile"})
    monkeypatch.setattr(
        "agent_os.bindings.resolve_backend_binding",
        lambda profile_name: current,
    )
    graph = ResumeBindingGraph(persisted)
    console, output = make_console()

    code = await async_main(
        ["--resume", "--thread-id", "same-binding"],
        graph_factory=lambda: graph,
        console=console,
    )

    assert code == 0
    assert graph.update_calls == []
    assert "WARNING" not in output.getvalue()


@pytest.mark.anyio
async def test_resume_conflict_without_force_keeps_checkpoint(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_OS_SANDBOX", str(tmp_path))
    current = resolve_backend_binding(None)
    persisted = current.model_copy(update={"architect": "old/architect"})
    graph = ResumeBindingGraph(persisted)
    console, output = make_console()

    code = await async_main(
        ["--resume", "--thread-id", "conflict"],
        graph_factory=lambda: graph,
        console=console,
    )

    assert code == 2
    assert graph.update_calls == []
    assert "Backend binding conflict" in output.getvalue()
    assert "architect" in output.getvalue()
    assert "--force-rebind" in output.getvalue()


@pytest.mark.anyio
async def test_force_rebind_updates_checkpoint_and_warns(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_OS_SANDBOX", str(tmp_path))
    current = resolve_backend_binding(None)
    persisted = current.model_copy(
        update={"architect": "old/architect", "executor": "old/executor"}
    )
    graph = ResumeBindingGraph(persisted)
    console, output = make_console()

    code = await async_main(
        ["--resume", "--force-rebind", "--thread-id", "forced"],
        graph_factory=lambda: graph,
        console=console,
    )

    assert code == 0
    assert graph.update_calls == [{"backend_binding": current}]
    assert "Forcing backend rebind" in output.getvalue()
    assert "architect" in output.getvalue()
    assert "executor" in output.getvalue()
    assert "Current resume node: architect" in output.getvalue()


@pytest.mark.anyio
async def test_force_rebind_at_executor_warns_about_partial_edits(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("AGENT_OS_SANDBOX", str(tmp_path))
    current = resolve_backend_binding(None)
    persisted = current.model_copy(update={"architect": "old/architect"})
    graph = ResumeBindingGraph(persisted, node="executor")
    console, output = make_console()

    code = await async_main(
        ["--resume", "--force-rebind", "--thread-id", "executor-rebind"],
        graph_factory=lambda: graph,
        console=console,
    )

    assert code == 0
    assert "Partial edits may exist" in output.getvalue()


@pytest.mark.anyio
async def test_force_rebind_unknown_adapter_leaves_checkpoint_unchanged(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("AGENT_OS_SANDBOX", str(tmp_path))
    monkeypatch.setenv("LLM_ARCHITECT", "cli/missing")
    persisted = BackendBinding(
        router=None,
        architect="old/architect",
        executor=None,
        profile_name=None,
        sandbox_root=str(tmp_path),
    )
    graph = ResumeBindingGraph(persisted)
    console, output = make_console()

    code = await async_main(
        ["--resume", "--force-rebind", "--thread-id", "unknown-adapter"],
        graph_factory=lambda: graph,
        console=console,
    )

    assert code == 2
    assert graph.update_calls == []
    assert "Unknown backend adapter 'missing'" in output.getvalue()


@pytest.mark.anyio
async def test_force_rebind_rejects_adapter_without_role(monkeypatch, tmp_path):
    class ArchitectOnlyAdapter:
        name = "architect-only"
        binary_name = "architect-only"
        supported_roles = frozenset({"architect"})
        stub = False

        def build_invoker(self, role):
            return MagicMock()

        def authentication_status(self):
            return AuthStatus(status="unknown")

    registry = BackendRegistry()
    registry.register(ArchitectOnlyAdapter())
    monkeypatch.setattr(
        "agent_os.backends.build_default_registry",
        lambda: registry,
    )
    monkeypatch.setenv("AGENT_OS_SANDBOX", str(tmp_path))
    monkeypatch.setenv("LLM_EXECUTOR", "cli/architect-only")
    persisted = BackendBinding(
        router=None,
        architect=None,
        executor="old/executor",
        profile_name=None,
        sandbox_root=str(tmp_path),
    )
    graph = ResumeBindingGraph(persisted)
    console, output = make_console()

    code = await async_main(
        ["--resume", "--force-rebind", "--thread-id", "wrong-role"],
        graph_factory=lambda: graph,
        console=console,
    )

    assert code == 2
    assert graph.update_calls == []
    rendered = " ".join(output.getvalue().split())
    assert "does not support role 'executor'" in rendered


@pytest.mark.anyio
async def test_force_rebind_requires_resume():
    console, output = make_console()

    code = await async_main(
        ["task", "--force-rebind"],
        graph_factory=FreshBindingGraph,
        console=console,
    )

    assert code == 2
    assert "--force-rebind flag requires --resume" in output.getvalue()


@pytest.mark.anyio
async def test_legacy_checkpoint_requires_force_rebind(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_OS_SANDBOX", str(tmp_path))
    graph = ResumeBindingGraph(None)
    console, output = make_console()

    code = await async_main(
        ["--resume", "--thread-id", "legacy"],
        graph_factory=lambda: graph,
        console=console,
    )

    assert code == 2
    assert graph.update_calls == []
    rendered = " ".join(output.getvalue().split())
    assert "Prior backend selection is not inferred" in rendered


@pytest.mark.anyio
async def test_legacy_checkpoint_force_rebind_attaches_current(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("AGENT_OS_SANDBOX", str(tmp_path))
    graph = ResumeBindingGraph(None)
    console, output = make_console()

    code = await async_main(
        ["--resume", "--force-rebind", "--thread-id", "legacy-force"],
        graph_factory=lambda: graph,
        console=console,
    )

    assert code == 0
    assert len(graph.update_calls) == 1
    assert isinstance(graph.binding, BackendBinding)
    assert (
        "Attaching current backend binding to a legacy checkpoint" in output.getvalue()
    )


def _paused_graph(checkpointer):
    def architect_node(state):
        return {
            "plan": ArchitectBrief(
                files=["demo.py"],
                changes=["add logging"],
                verify_cmd="pytest",
            )
        }

    def executor_node(state):
        return {
            "executor_output": ExecutorReport(
                diff="updated demo.py",
                verify_output="passed",
                success=True,
            )
        }

    def dispatcher_node(state):
        return Command(update={"router_escalated": True}, goto="supervisor")

    return build_graph(
        architect_node_impl=architect_node,
        executor_node_impl=executor_node,
        tool_dispatcher_node_impl=dispatcher_node,
        checkpointer=checkpointer,
    )


def _seed_state(binding=...):
    state = {
        "messages": [],
        "task": "add logging",
        "plan": None,
        "executor_output": None,
        "human_feedback": None,
        "hot_context": None,
    }
    if binding is not ...:
        state["backend_binding"] = binding
    return state


@pytest.mark.anyio
async def test_force_rebind_updates_real_sqlite_checkpoint(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_OS_SANDBOX", str(tmp_path / "sandbox"))
    current = resolve_backend_binding(None)
    persisted = current.model_copy(update={"architect": "old/architect"})
    config = build_runtime_config("sqlite-binding")
    database = tmp_path / "binding.db"

    async with AsyncSqliteSaver.from_conn_string(str(database)) as saver:
        saver.serde = get_checkpoint_serializer()
        graph = _paused_graph(saver)
        await graph.ainvoke(_seed_state(persisted), config=config)
        console, output = make_console()

        code = await async_main(
            ["--resume", "--force-rebind", "--thread-id", "sqlite-binding"],
            graph_factory=lambda: graph,
            input_fn=lambda _: "approved",
            console=console,
        )
        snapshot = await graph.aget_state(config)

    assert code == 0
    assert snapshot.next == ()
    assert snapshot.values["backend_binding"] == current
    assert "Forcing backend rebind" in output.getvalue()


@pytest.mark.anyio
async def test_force_rebind_attaches_binding_to_real_legacy_checkpoint(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("AGENT_OS_SANDBOX", str(tmp_path / "sandbox"))
    current = resolve_backend_binding(None)
    config = build_runtime_config("sqlite-legacy-binding")
    database = tmp_path / "legacy-binding.db"

    async with AsyncSqliteSaver.from_conn_string(str(database)) as saver:
        saver.serde = get_checkpoint_serializer()
        graph = _paused_graph(saver)
        await graph.ainvoke(_seed_state(), config=config)
        console, output = make_console()

        code = await async_main(
            [
                "--resume",
                "--force-rebind",
                "--thread-id",
                "sqlite-legacy-binding",
            ],
            graph_factory=lambda: graph,
            input_fn=lambda _: "approved",
            console=console,
        )
        snapshot = await graph.aget_state(config)

    assert code == 0
    assert snapshot.next == ()
    assert snapshot.values["backend_binding"] == current
    assert "legacy checkpoint" in output.getvalue()
