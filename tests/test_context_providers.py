import asyncio
import time
from pathlib import Path

import pytest

from agent_os.context import ContextBlock, ContextProvider, load_injected_context
from agent_os.policy import LocalPolicy
from agent_os.principal import Principal
from agent_os.schemas import ActionProposal, PolicyDecision
from agent_os.workspace import WorkspaceLoadError, compose_workspace, load_workspace


class StaticContextProvider:
    name = "static_provider"

    def provide(
        self,
        task: str,
        *,
        budget_chars: int,
        principal: Principal,
        workspace_id: str,
    ):
        return ContextBlock(
            source="static_provider",
            title="Overview",
            content=f"Facts for {workspace_id} ({principal.id}): budget={budget_chars}",
            metadata={"tier": 1},
        )


class SlowContextProvider:
    name = "slow_provider"

    def provide(
        self,
        task: str,
        *,
        budget_chars: int = 1000,
        principal: Principal | None = None,
        workspace_id: str = "default",
    ):
        time.sleep(1.0)
        return "Slow context that took too long"


class ErrorContextProvider:
    name = "error_provider"

    def provide(
        self,
        task: str,
        *,
        budget_chars: int = 1000,
        principal: Principal | None = None,
        workspace_id: str = "default",
    ):
        raise RuntimeError("Context provider backend failed")


class SyntheticEntryPoint:
    def __init__(self, name: str, target: object = None, error: Exception | None = None):
        self.name = name
        self.target = target
        self.error = error

    def load(self):
        if self.error:
            raise self.error
        return self.target


def test_context_provider_protocol_and_block():
    provider = StaticContextProvider()
    assert isinstance(provider, ContextProvider)
    assert provider.name == "static_provider"

    block = provider.provide(
        "test task",
        budget_chars=500,
        principal=Principal(id="user:test", kind="human", display="Test User"),
        workspace_id="ws-1",
    )
    assert isinstance(block, ContextBlock)
    assert block.source == "static_provider"
    assert "Facts for ws-1" in block.content


@pytest.mark.anyio
async def test_load_injected_context_bounded_budget():
    providers = [StaticContextProvider(), StaticContextProvider()]
    injected = await load_injected_context(providers, "build feature", max_chars=80)
    assert len(injected) <= 80
    assert "--- BEGIN CONTEXT" in injected


@pytest.mark.anyio
async def test_load_injected_context_async_timeout_non_blocking():
    ticks = 0

    async def ticker():
        nonlocal ticks
        for _ in range(20):
            await asyncio.sleep(0.01)
            ticks += 1

    ticker_task = asyncio.create_task(ticker())

    start_time = time.perf_counter()
    injected = await load_injected_context(
        [SlowContextProvider()],
        "task",
        timeout_seconds=0.1,
    )
    elapsed = time.perf_counter() - start_time

    await ticker_task

    assert injected == ""
    assert elapsed < 0.5  # Materially less than 1.0s
    assert ticks >= 5  # Concurrent event loop task made progress during wait


@pytest.mark.anyio
async def test_load_injected_context_error_is_fail_soft():
    injected = await load_injected_context(
        [ErrorContextProvider(), StaticContextProvider()],
        "task",
    )
    assert "Facts for" in injected


@pytest.mark.anyio
async def test_load_injected_context_respects_policy_denial():
    class DenyContextProviderPolicy(LocalPolicy):
        def evaluate(
            self, proposal: ActionProposal, *, workspace=None, context=None
        ) -> PolicyDecision:
            if "static_provider" in proposal.tool:
                return PolicyDecision(
                    decision="deny", policy_id="deny-ctx", reason="Denied context"
                )
            return PolicyDecision(decision="allow", policy_id="allow-all", reason="Allow")

    policy = DenyContextProviderPolicy()
    injected = await load_injected_context(
        [StaticContextProvider()], "task", policy=policy
    )
    assert injected == ""


@pytest.mark.anyio
async def test_load_injected_context_provenance_only_no_content_in_ledger(
    tmp_path: Path, monkeypatch
):
    recorded_events = []
    monkeypatch.setattr(
        "agent_os.runs.append_event",
        lambda run_id, kind, payload: recorded_events.append((run_id, kind, payload)),
    )

    injected = await load_injected_context(
        [StaticContextProvider()],
        "task",
        run_id="run-12345",
        principal=Principal(id="service:test", kind="service", display="Test Service"),
        workspace_id="sec-ws",
    )
    assert "Facts for sec-ws" in injected
    assert len(recorded_events) == 1
    run_id, kind, payload = recorded_events[0]
    assert run_id == "run-12345"
    assert kind == "context"
    assert payload["provider"] == "static_provider"
    assert payload["source"] == "static_provider"
    assert payload["title"] == "Overview"
    assert payload["chars"] > 0
    # INVARIANT: Content or secret must NOT be persisted to event payload
    assert "content" not in payload
    assert "metadata" not in payload
    assert "Facts for sec-ws" not in str(payload)


def test_workspace_context_provider_discovery(tmp_path: Path, monkeypatch):
    eps = [SyntheticEntryPoint("custom_ctx", target=StaticContextProvider)]
    monkeypatch.setattr(
        "agent_os.plugins._get_entry_points",
        lambda group: eps if group == "agent_os.context_providers" else [],
    )

    workspace_path = tmp_path / "workspace.toml"
    workspace_path.write_text(
        """
[workspace]
name = "ctx-ws"

[backends]
architect = "codex"
executor = "codex"

[memory]
type = "markdown"
path = "."

[context]
providers = ["custom_ctx"]
""",
        encoding="utf-8",
    )

    ws = load_workspace(workspace_path)
    composed = compose_workspace(ws)
    assert len(composed.context_providers) == 1
    assert composed.context_providers[0].name == "static_provider"


def test_workspace_context_provider_cannot_override_builtin(tmp_path: Path, monkeypatch):
    eps = [SyntheticEntryPoint("hot_context", target=StaticContextProvider)]
    monkeypatch.setattr(
        "agent_os.plugins._get_entry_points",
        lambda group: eps if group == "agent_os.context_providers" else [],
    )

    workspace_path = tmp_path / "workspace.toml"
    workspace_path.write_text(
        """
[workspace]
name = "bad-ctx-ws"

[backends]
architect = "codex"
executor = "codex"

[memory]
type = "markdown"
path = "."

[context]
providers = ["hot_context"]
""",
        encoding="utf-8",
    )

    with pytest.raises(
        WorkspaceLoadError, match="cannot override built-in context provider 'hot_context'"
    ):
        load_workspace(workspace_path)
