import io
from types import SimpleNamespace

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command
from pydantic import BaseModel
from rich.console import Console

from agent_os.cli.app import _stream_pass
from agent_os.cli.formatter import EventFormatter
from agent_os.connectors import MarkdownVaultConnector
from agent_os.memory_gate import gated_write
from agent_os.permission_store import (
    InMemoryPermissionStore,
    PermissionRule,
    SqlitePermissionStore,
)
from agent_os.policy import LocalPolicy, derive_permission_key
from agent_os.routing import build_runtime_config
from agent_os.schemas import ActionProposal, MemoryWriteProposal


@pytest.mark.anyio
async def test_cli_stream_binds_workspace_policy_to_nested_memory_write(
    tmp_path,
) -> None:
    """A real CLI graph stream reaches gated_write with the composed policy."""
    connector = MarkdownVaultConnector(str(tmp_path / "vault"))
    proposal = MemoryWriteProposal(
        connector="untrusted-display-name",
        ref="AI/Decisions/runtime.md",
        mode="create",
        content_preview="runtime policy wiring",
        side_effect="write",
    )
    action = ActionProposal(
        tool="memory.write",
        connector="markdown_vault",
        arguments={"ref": proposal.ref, "mode": proposal.mode},
        side_effect="write",
    )
    key = derive_permission_key(action)
    assert key is not None

    store = InMemoryPermissionStore()
    store.upsert(
        PermissionRule(
            permission_key=key,
            effect="always_approve",
            tier_at_creation="low",
        )
    )
    runtime = SimpleNamespace(policy=LocalPolicy(store=store))
    config = build_runtime_config("cli-runtime-policy")
    config["configurable"]["workspace"] = runtime

    class RuntimeState(BaseModel):
        task: str
        committed: bool = False

    results = []
    builder = StateGraph(RuntimeState)

    def write_node(state: RuntimeState) -> dict[str, object]:
        result = gated_write(connector, proposal, "runtime content")
        results.append(result)
        return {"committed": result.committed}

    builder.add_node("write", write_node)
    builder.add_edge(START, "write")
    builder.add_edge("write", END)
    graph = builder.compile(checkpointer=InMemorySaver())
    formatter = EventFormatter(
        console=Console(file=io.StringIO(), force_terminal=False, color_system=None)
    )

    await _stream_pass(graph, {"task": "write"}, config, formatter, verbose=False)

    assert len(results) == 1
    assert results[0].committed is True
    stored = store.get(key)
    assert stored is not None
    assert stored.approve_count == 1


@pytest.mark.anyio
async def test_server_run_binds_workspace_policy_to_nested_memory_write(
    tmp_path,
    monkeypatch,
) -> None:
    """The HTTP run executor supplies the active workspace policy to actions."""
    from agent_os import runs
    from agent_os.checkpoints import CHECKPOINT_DB_ENV
    from agent_os.server import run_executor, runtime

    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    workspace_path = workspace_root / "workspace.toml"
    workspace_path.write_text(
        '[workspace]\nname = "server-runtime"\n', encoding="utf-8"
    )
    sandbox_root = tmp_path / "sandbox"
    sandbox_root.mkdir()
    checkpoint_db = tmp_path / "checkpoints.db"
    runs_db = tmp_path / "runs.db"

    monkeypatch.setenv("AGENT_OS_WORKSPACE", str(workspace_path))
    monkeypatch.setenv("AGENT_OS_SANDBOX", str(sandbox_root))
    monkeypatch.setenv(CHECKPOINT_DB_ENV, str(checkpoint_db))
    monkeypatch.setenv("AGENT_OS_RUNS_DB", str(runs_db))
    runtime.composed_workspace.cache_clear()

    connector = MarkdownVaultConnector(str(sandbox_root))
    proposal = MemoryWriteProposal(
        connector="markdown_vault",
        ref="AI/Decisions/server-runtime.md",
        mode="create",
        content_preview="server runtime wiring",
        side_effect="write",
    )
    action = ActionProposal(
        tool="memory.write",
        connector="markdown_vault",
        arguments={"ref": proposal.ref, "mode": proposal.mode},
        side_effect="write",
    )
    key = derive_permission_key(action)
    assert key is not None
    workspace_store = SqlitePermissionStore(str(workspace_root / "permissions.db"))
    workspace_store.upsert(
        PermissionRule(
            permission_key=key,
            effect="always_approve",
            tier_at_creation="low",
        )
    )

    class Snapshot:
        values: dict[str, object] = {}
        tasks: tuple[()] = ()

    class Graph:
        result = None

        async def astream_events(self, graph_input, config, version):
            self.result = gated_write(connector, proposal, "server runtime content")
            if False:
                yield {}

        async def aget_state(self, config):
            return Snapshot()

    graph = Graph()
    monkeypatch.setattr(
        run_executor,
        "build_runtime_graph",
        lambda *, checkpointer: graph,
    )
    monkeypatch.setattr(
        run_executor,
        "initial_state",
        lambda task, **_: {"task": task},
    )

    try:
        run_id = runs.create_run("server-runtime-thread", None, "write")
        await run_executor.execute_run(run_id, "server-runtime-thread", "write")

        assert graph.result is not None
        assert graph.result.committed is True
        assert (sandbox_root / proposal.ref).exists()
        assert runs.get_run(run_id)["status"] == "completed"
    finally:
        runtime.composed_workspace.cache_clear()


@pytest.mark.anyio
async def test_cli_without_workspace_persists_and_reapplies_memory_rule(
    tmp_path,
    monkeypatch,
) -> None:
    """Standalone CLI users get the default durable permission store too."""
    monkeypatch.setenv("AGENT_OS_PERMISSIONS_DB", str(tmp_path / "permissions.db"))
    connector = MarkdownVaultConnector(str(tmp_path / "vault"))
    proposal = MemoryWriteProposal(
        connector="markdown_vault",
        ref="AI/Decisions/standalone.md",
        mode="append",
        content_preview="standalone policy wiring",
        side_effect="write",
    )

    class RuntimeState(BaseModel):
        task: str
        committed: bool = False

    first_results = []
    first_builder = StateGraph(RuntimeState)

    def first_write(state: RuntimeState) -> dict[str, object]:
        result = gated_write(connector, proposal, "first")
        first_results.append(result)
        return {"committed": result.committed}

    first_builder.add_node("write", first_write)
    first_builder.add_edge(START, "write")
    first_builder.add_edge("write", END)
    first_graph = first_builder.compile(checkpointer=InMemorySaver())
    formatter = EventFormatter(
        console=Console(file=io.StringIO(), force_terminal=False, color_system=None)
    )
    first_config = build_runtime_config("standalone-learning-first")

    await _stream_pass(
        first_graph, {"task": "write"}, first_config, formatter, verbose=False
    )
    assert first_results == []

    await _stream_pass(
        first_graph,
        Command(resume="always_approve"),
        first_config,
        formatter,
        verbose=False,
    )
    assert len(first_results) == 1
    assert first_results[0].committed is True

    # A new graph/thread opens a fresh policy/store instance and still applies
    # the durable exact rule without prompting.
    second_results = []
    second_builder = StateGraph(RuntimeState)

    def second_write(state: RuntimeState) -> dict[str, object]:
        result = gated_write(connector, proposal, "second")
        second_results.append(result)
        return {"committed": result.committed}

    second_builder.add_node("write", second_write)
    second_builder.add_edge(START, "write")
    second_builder.add_edge("write", END)
    second_graph = second_builder.compile(checkpointer=InMemorySaver())
    await _stream_pass(
        second_graph,
        {"task": "write again"},
        build_runtime_config("standalone-learning-second"),
        formatter,
        verbose=False,
    )
    assert len(second_results) == 1
    assert second_results[0].committed is True
