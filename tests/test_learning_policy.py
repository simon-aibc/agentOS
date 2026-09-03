from typing import Any
from unittest.mock import MagicMock

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command
from pydantic import BaseModel
from rich.console import Console

from agent_os.cli.app import async_main
from agent_os.connectors import MarkdownVaultConnector
from agent_os.memory_gate import gated_write
from agent_os.permission_store import (
    InMemoryPermissionStore,
    PermissionRule,
    SqlitePermissionStore,
)
from agent_os.policy import (
    LocalPolicy,
    apply_policy,
    derive_permission_key,
)
from agent_os.schemas import ActionProposal, ExecutionResult, MemoryWriteProposal


def _memory_action(
    ref: str = "AI/Decisions/test.md",
    *,
    mode: str = "create",
    connector: str = "markdown_vault",
) -> ActionProposal:
    """Build an action that is eligible for a safely scoped learned rule."""
    return ActionProposal(
        tool="memory.write",
        connector=connector,
        arguments={"ref": ref, "mode": mode},
        side_effect="write",
    )


def _permission_key(proposal: ActionProposal) -> str:
    key = derive_permission_key(proposal)
    assert key is not None
    return key


# ==============================================================================
# 1. Backward Compatibility & Default Taxonomy
# ==============================================================================


def test_1_store_none_backward_compat():
    """store=None -> evaluate() returns identical results with default taxonomy across side_effects."""
    policy = LocalPolicy()

    # read -> allow
    p_read = ActionProposal(tool="file.read", side_effect="read")
    assert policy.evaluate(p_read).decision == "allow"

    # write -> require_approval
    p_write = ActionProposal(tool="file.write", side_effect="write")
    assert policy.evaluate(p_write).decision == "require_approval"

    # network -> require_approval
    p_net = ActionProposal(tool="http.get", side_effect="network")
    assert policy.evaluate(p_net).decision == "require_approval"

    # payment -> deny
    p_pay = ActionProposal(tool="stripe.charge", side_effect="payment")
    assert policy.evaluate(p_pay).decision == "deny"

    # privileged -> deny
    p_priv = ActionProposal(tool="system.reboot", side_effect="privileged")
    assert policy.evaluate(p_priv).decision == "deny"


# ==============================================================================
# 2. Rule Evaluation & Hit Counting
# ==============================================================================


def test_2_always_approve_hit():
    """always_approve hit -> allow, approve_count increases by 1."""
    store = InMemoryPermissionStore()
    policy = LocalPolicy(store=store)
    proposal = _memory_action()
    perm_key = _permission_key(proposal)

    rule = PermissionRule(
        permission_key=perm_key, effect="always_approve", tier_at_creation="low"
    )
    store.upsert(rule)

    decision = policy.evaluate(proposal)
    assert decision.decision == "allow"
    assert decision.policy_id == "learned"
    assert decision.reason == "always_approve"

    stored_rule = store.get(perm_key)
    assert stored_rule is not None
    assert stored_rule.approve_count == 1
    assert stored_rule.deny_count == 0


def test_3_always_deny_hit():
    """always_deny hit -> deny, deny_count increases by 1."""
    store = InMemoryPermissionStore()
    policy = LocalPolicy(store=store)
    proposal = _memory_action()
    perm_key = _permission_key(proposal)

    rule = PermissionRule(
        permission_key=perm_key, effect="always_deny", tier_at_creation="low"
    )
    store.upsert(rule)

    decision = policy.evaluate(proposal)
    assert decision.decision == "deny"
    assert decision.policy_id == "learned"
    assert decision.reason == "always_deny"

    stored_rule = store.get(perm_key)
    assert stored_rule is not None
    assert stored_rule.deny_count == 1
    assert stored_rule.approve_count == 0


def test_4_always_deny_wins_over_always_approve():
    """If rule effect is always_deny, it overrides even if previously set (or updated via upsert)."""
    store = InMemoryPermissionStore()
    policy = LocalPolicy(store=store)
    proposal = _memory_action()
    perm_key = _permission_key(proposal)

    # First upsert always_approve
    rule_app = PermissionRule(
        permission_key=perm_key, effect="always_approve", tier_at_creation="low"
    )
    store.upsert(rule_app)

    # Second upsert always_deny
    rule_deny = PermissionRule(
        permission_key=perm_key, effect="always_deny", tier_at_creation="low"
    )
    store.upsert(rule_deny)

    decision = policy.evaluate(proposal)
    assert decision.decision == "deny"
    assert decision.reason == "always_deny"


# ==============================================================================
# 3. Session Approval Lifecycle & Isolation
# ==============================================================================


def test_5_session_approval_lifecycle():
    """session approve -> allow; after clear_session -> fallback to require_approval."""
    session_key = "sess_123"
    policy = LocalPolicy(session_key=session_key)
    proposal = _memory_action("AI/Decisions/session.md")
    perm_key = _permission_key(proposal)

    # Initially require_approval
    assert policy.evaluate(proposal).decision == "require_approval"

    # Approve session
    LocalPolicy.approve_session(session_key, perm_key)
    dec = policy.evaluate(proposal)
    assert dec.decision == "allow"
    assert dec.policy_id == "session"

    # Clear session
    LocalPolicy.clear_session(session_key)
    assert policy.evaluate(proposal).decision == "require_approval"


def test_session_isolation_with_session():
    """with_session creates scoped policy without mutating base or leaking across sessions."""
    store = InMemoryPermissionStore()
    base_policy = LocalPolicy(store=store, mode="manual")

    policy_a = base_policy.with_session("sess_a")
    policy_b = base_policy.with_session("sess_b")

    proposal = _memory_action("AI/Decisions/shared.md")
    perm_key = _permission_key(proposal)

    LocalPolicy.approve_session("sess_a", perm_key)

    # session A allows
    assert policy_a.evaluate(proposal).decision == "allow"
    # session B requires approval (no leak!)
    assert policy_b.evaluate(proposal).decision == "require_approval"
    # base without session requires approval
    assert base_policy.evaluate(proposal).decision == "require_approval"

    LocalPolicy.clear_session("sess_a")


# ==============================================================================
# 4. Mode Semantics (manual, smart, off)
# ==============================================================================


def test_6_mode_off():
    """mode='off' -> unsafe local-only escape hatch: allows all actions including payment."""
    policy = LocalPolicy(mode="off")
    proposal = ActionProposal(tool="stripe.charge", side_effect="payment")

    decision = policy.evaluate(proposal)
    assert decision.decision == "allow"
    assert decision.policy_id == "mode-off"


def test_mode_smart_is_manual_alias():
    """mode='smart' operates as an alias of 'manual' in v2.0.1."""
    store = InMemoryPermissionStore()
    policy_smart = LocalPolicy(store=store, mode="smart")
    policy_manual = LocalPolicy(store=store, mode="manual")

    p_write = ActionProposal(
        tool="file.write", side_effect="write", arguments={"ref": "file.txt"}
    )
    p_pay = ActionProposal(tool="stripe.charge", side_effect="payment")

    assert (
        policy_smart.evaluate(p_write).decision
        == policy_manual.evaluate(p_write).decision
        == "require_approval"
    )
    assert (
        policy_smart.evaluate(p_pay).decision
        == policy_manual.evaluate(p_pay).decision
        == "deny"
    )


def test_mode_invalid_fallback():
    """Invalid mode logs warning and falls back to 'manual'."""
    policy = LocalPolicy(mode="nonexistent_mode")
    assert policy.mode == "manual"
    assert (
        policy.evaluate(ActionProposal(tool="f", side_effect="write")).decision
        == "require_approval"
    )


# ==============================================================================
# 5. High-Risk Guard & Key Derivation
# ==============================================================================


def test_7_tier_high_guard(monkeypatch):
    """tier-high guard: apply_policy with side_effect='payment', feedback='always_approve' -> NO rule created in store."""
    store = InMemoryPermissionStore()
    engine = LocalPolicy(store=store)

    # Mock interrupt to return "always_approve"
    monkeypatch.setattr("agent_os.policy.interrupt", lambda msg: "always_approve")

    execute_fn = MagicMock(
        return_value=ExecutionResult(
            status="completed", outputs={}, artifacts=[], errors=[]
        )
    )

    p_high_req = ActionProposal(tool="custom.high_tool", side_effect="payment")
    monkeypatch.setattr(
        engine,
        "evaluate",
        lambda prop, **kw: MagicMock(decision="require_approval", reason="testing"),
    )

    res = apply_policy(engine, p_high_req, execute_fn=execute_fn)
    assert res.status == "completed"
    assert len(store.list()) == 0  # No rule created in store


def test_8_sqlite_persistence(tmp_path):
    """SqlitePermissionStore persistence test across connections and WAL settings."""
    db_file = str(tmp_path / "permissions.db")
    store1 = SqlitePermissionStore(db_file)

    rule = PermissionRule(
        permission_key="memory_write:write:markdown_vault:create:AI/Decisions/config.md",
        effect="always_approve",
        tier_at_creation="low",
    )
    store1.upsert(rule)

    # Open store2 on same DB file (simulating process restart)
    store2 = SqlitePermissionStore(db_file)
    fetched = store2.get(
        "memory_write:write:markdown_vault:create:AI/Decisions/config.md"
    )
    assert fetched is not None
    assert fetched.effect == "always_approve"
    assert fetched.tier_at_creation == "low"


def test_9_derive_permission_key_is_exact_memory_scope():
    """Only memory writes with connector, mode, and ref can be remembered."""
    p1 = _memory_action("AI/Decisions/foo/bar.md", mode="create")
    key1 = _permission_key(p1)
    assert key1 == "memory_write:write:markdown_vault:create:AI/Decisions/foo/bar.md"

    # Mode and connector are independently scoped, so create never grants overwrite
    # and one connector never grants a second provider.
    assert (
        _permission_key(_memory_action("AI/Decisions/foo/bar.md", mode="overwrite"))
        != key1
    )
    assert (
        _permission_key(_memory_action("AI/Decisions/foo/bar.md", connector="gbrain"))
        != key1
    )

    # Generic tools lack a canonical destination schema in this release.
    assert (
        derive_permission_key(
            ActionProposal(tool="http.request", side_effect="network")
        )
        is None
    )
    assert (
        derive_permission_key(
            ActionProposal(
                tool="file.write", side_effect="write", arguments={"ref": "foo.txt"}
            )
        )
        is None
    )
    assert derive_permission_key(_memory_action("AI/Decisions/a:b.md")) is None
    assert derive_permission_key(_memory_action("AI/Decisions/a b.md")) is None
    assert (
        derive_permission_key(
            _memory_action("AI/Decisions/foo.md", connector="markdown-vault")
        )
        is None
    )
    assert (
        derive_permission_key(_memory_action("AI/Decisions/foo.md", mode="Create"))
        is None
    )

    store = InMemoryPermissionStore()
    store.upsert(
        PermissionRule(
            permission_key=key1, effect="always_approve", tier_at_creation="low"
        )
    )
    assert store.get(key1) is not None


def test_store_delete_boolean():
    """Test delete returns True on deletion and False when rule not found."""
    store = InMemoryPermissionStore()
    k = "tool:write:file.txt"
    assert store.delete(k) is False

    store.upsert(
        PermissionRule(
            permission_key=k, effect="always_approve", tier_at_creation="low"
        )
    )
    assert store.delete(k) is True
    assert store.delete(k) is False


def test_10_workspace_permission_store_composition(monkeypatch, tmp_path):
    """monkeypatch env AGENT_OS_PERMISSIONS_DB -> tmp_path, build workspace, assert policy.store is not None."""
    from agent_os.workspace import Workspace, WorkspaceMeta, compose_workspace

    db_path = str(tmp_path / "custom_perm.db")
    monkeypatch.setenv("AGENT_OS_PERMISSIONS_DB", db_path)

    ws = Workspace(
        workspace=WorkspaceMeta(name="test_ws"),
        backends={"router": "mock/dummy"},
    )
    composed = compose_workspace(ws)
    assert composed.policy.store is not None
    assert isinstance(composed.policy.store, SqlitePermissionStore)
    assert composed.policy.store.db_path == db_path


def test_11_workspace_permission_store_fallback(monkeypatch):
    """Assert store initialization error falls back to LocalPolicy without store (policy.store is None)."""
    from agent_os.workspace import Workspace, WorkspaceMeta, compose_workspace

    # Invalid path / uncreatable directory on Unix
    invalid_db_path = "/invalid_dir_no_perm/permissions.db"
    monkeypatch.setenv("AGENT_OS_PERMISSIONS_DB", invalid_db_path)

    ws = Workspace(
        workspace=WorkspaceMeta(name="test_ws_fallback"),
        backends={"router": "mock/dummy"},
    )
    composed = compose_workspace(ws)
    assert composed.policy.store is None


# ==============================================================================
# 6. Gated Write Explicit Conversion Contract Tests
# ==============================================================================


def test_gated_write_contract_success(tmp_path):
    """gated_write returns MemoryWriteResult with committed=True when auto-approved or approved."""
    connector = MarkdownVaultConnector(str(tmp_path))
    proposal = MemoryWriteProposal(
        connector="markdown_vault",
        ref="AI/Logs/2026-08-18.md",
        mode="append",
        content_preview="Log entry",
        side_effect="write",
    )
    res = gated_write(connector, proposal, "Hello world")
    assert res.committed is True
    assert res.bytes_written is not None
    assert res.ref == "AI/Logs/2026-08-18.md"


def test_gated_write_contract_rejected_and_cancelled(tmp_path, monkeypatch):
    """gated_write returns committed=False and bytes_written=None on rejection/cancel."""
    connector = MarkdownVaultConnector(str(tmp_path))
    proposal = MemoryWriteProposal(
        connector="markdown_vault",
        ref="AI/Decisions/test.md",
        mode="create",
        content_preview="Decision",
        side_effect="write",
    )
    monkeypatch.setattr("agent_os.policy.interrupt", lambda x: "rejected: not allowed")
    res = gated_write(connector, proposal, "Decision content")
    assert res.committed is False
    assert res.bytes_written is None
    assert res.ref == "AI/Decisions/test.md"


def test_gated_write_reuses_provided_engine(tmp_path):
    """gated_write reuses the provided engine with its session approvals."""
    connector = MarkdownVaultConnector(str(tmp_path))
    session_key = "test-session"
    policy = LocalPolicy(session_key=session_key)

    proposal = MemoryWriteProposal(
        connector="markdown_vault",
        ref="AI/Decisions/doc.md",
        mode="create",
        content_preview="doc",
        side_effect="write",
    )
    perm_key = _permission_key(
        _memory_action("AI/Decisions/doc.md", mode="create", connector="markdown_vault")
    )
    LocalPolicy.approve_session(session_key, perm_key)

    # With the engine reusing session_key, it auto-writes without interrupt
    res = gated_write(connector, proposal, "Doc content", engine=policy)
    assert res.committed is True

    LocalPolicy.clear_session(session_key)


def test_memory_write_mode_is_strictly_validated(tmp_path):
    """A near-match mode cannot reuse a create/overwrite permission scope."""
    connector = MarkdownVaultConnector(str(tmp_path))
    with pytest.raises(ValueError, match="mode"):
        MemoryWriteProposal(
            connector="markdown_vault",
            ref="AI/Decisions/mode.md",
            mode="Create",
            content_preview="invalid mode",
            side_effect="write",
        )

    # Defend at the execution seam too if an unvalidated model is injected.
    unvalidated = MemoryWriteProposal.model_construct(
        connector="markdown_vault",
        ref="AI/Decisions/mode.md",
        mode="create ",
        content_preview="invalid mode",
        side_effect="write",
    )
    with pytest.raises(ValueError, match="Unsupported memory write mode"):
        gated_write(connector, unvalidated, "content")


# ==============================================================================
# 7. Real LangGraph Interrupt/Resume StateGraph E2E Tests
# ==============================================================================


class WorkflowState(BaseModel):
    task: str
    result: str | None = None
    status: str = "running"


def _build_test_action_graph(engine: LocalPolicy, proposal: ActionProposal):
    """Build a real LangGraph StateGraph that evaluates the action through apply_policy."""
    checkpointer = InMemorySaver()
    builder = StateGraph(WorkflowState)

    def execute_node(state: WorkflowState) -> dict[str, Any]:
        def _exec(p: ActionProposal) -> ExecutionResult:
            return ExecutionResult(
                status="completed", outputs={"data": "executed_payload"}
            )

        res = apply_policy(engine, proposal, execute_fn=_exec)
        if res.status == "completed":
            return {"result": str(res.outputs.get("data")), "status": "completed"}
        return {"result": None, "status": res.status}

    builder.add_node("action_node", execute_node)
    builder.add_edge(START, "action_node")
    builder.add_edge("action_node", END)

    return builder.compile(checkpointer=checkpointer)


def test_e2e_a_approve_once_succeeds_and_next_action_asks_again():
    """(a) approve once -> succeeds; next matching action interrupts again."""
    engine = LocalPolicy()
    proposal = ActionProposal(
        tool="file.write", side_effect="write", arguments={"ref": "doc.txt"}
    )
    graph = _build_test_action_graph(engine, proposal)

    config_1 = {"configurable": {"thread_id": "thread-1"}}
    paused_1 = graph.invoke({"task": "write doc"}, config=config_1)
    assert "__interrupt__" in paused_1

    # Resume with "approved"
    res_1 = graph.invoke(Command(resume="approved"), config=config_1)
    assert res_1["status"] == "completed"
    assert res_1["result"] == "executed_payload"

    # Second matching action in a new thread
    config_2 = {"configurable": {"thread_id": "thread-2"}}
    paused_2 = graph.invoke({"task": "write doc again"}, config=config_2)
    assert "__interrupt__" in paused_2  # Asks again!


def test_e2e_b_session_auto_allows_only_in_same_session():
    """(b) session -> auto-allows only in same session; new session prompts again."""
    session_id = "session-unique-xyz"
    engine_a = LocalPolicy(session_key=session_id)
    proposal = _memory_action("AI/Decisions/session_file.md")

    graph_a = _build_test_action_graph(engine_a, proposal)
    config_a1 = {"configurable": {"thread_id": "thread-session-1"}}

    paused = graph_a.invoke({"task": "write file"}, config=config_a1)
    assert "__interrupt__" in paused

    # Resume with "session"
    res = graph_a.invoke(Command(resume="session"), config=config_a1)
    assert res["status"] == "completed"

    # Next matching action in the SAME session -> auto-allows without interrupt!
    config_a2 = {"configurable": {"thread_id": "thread-session-2"}}
    res_same = graph_a.invoke({"task": "write file 2"}, config=config_a2)
    assert "__interrupt__" not in res_same
    assert res_same["status"] == "completed"

    # Third action in a DIFFERENT session -> prompts again!
    engine_other = LocalPolicy(session_key="other-session-999")
    graph_other = _build_test_action_graph(engine_other, proposal)
    config_other = {"configurable": {"thread_id": "thread-other"}}
    paused_other = graph_other.invoke({"task": "write file 3"}, config=config_other)
    assert "__interrupt__" in paused_other

    LocalPolicy.clear_session(session_id)


def test_e2e_c_always_approve_persists_and_auto_allows_after_restart(tmp_path):
    """(c) always approve -> persists in SQLite and auto-allows after process restart."""
    db_file = str(tmp_path / "permissions.db")
    store1 = SqlitePermissionStore(db_file)
    engine1 = LocalPolicy(store=store1)
    proposal = _memory_action("AI/Decisions/schema.md")

    graph1 = _build_test_action_graph(engine1, proposal)
    config1 = {"configurable": {"thread_id": "proc1-thread"}}

    paused = graph1.invoke({"task": "run migration"}, config=config1)
    assert "__interrupt__" in paused

    # Resume with "always_approve"
    res1 = graph1.invoke(Command(resume="always_approve"), config=config1)
    assert res1["status"] == "completed"

    # Simulate Process Restart: instantiate fresh Store2 and Engine2 on the same SQLite DB
    store2 = SqlitePermissionStore(db_file)
    engine2 = LocalPolicy(store=store2)
    graph2 = _build_test_action_graph(engine2, proposal)
    config2 = {"configurable": {"thread_id": "proc2-thread"}}

    # Next matching action runs with ZERO interrupts
    res2 = graph2.invoke({"task": "run migration after restart"}, config=config2)
    assert "__interrupt__" not in res2
    assert res2["status"] == "completed"

    stored = store2.get(_permission_key(proposal))
    assert stored is not None
    assert stored.approve_count == 1


def test_e2e_d_always_deny_persists_and_denies_after_restart(tmp_path):
    """(d) always deny -> persists and auto-denies after restart."""
    db_file = str(tmp_path / "permissions.db")
    store1 = SqlitePermissionStore(db_file)
    engine1 = LocalPolicy(store=store1)
    proposal = _memory_action("AI/Decisions/users.md")

    graph1 = _build_test_action_graph(engine1, proposal)
    config1 = {"configurable": {"thread_id": "proc1-thread-deny"}}

    paused = graph1.invoke({"task": "drop table"}, config=config1)
    assert "__interrupt__" in paused

    # Resume with "always_deny"
    res1 = graph1.invoke(Command(resume="always_deny"), config=config1)
    assert res1["status"] == "cancelled"

    # Restart
    store2 = SqlitePermissionStore(db_file)
    engine2 = LocalPolicy(store=store2)
    graph2 = _build_test_action_graph(engine2, proposal)
    config2 = {"configurable": {"thread_id": "proc2-thread-deny"}}

    res2 = graph2.invoke({"task": "drop table again"}, config=config2)
    assert "__interrupt__" not in res2
    assert res2["status"] == "failed"  # Policy evaluated to deny!

    stored = store2.get(_permission_key(proposal))
    assert stored is not None
    assert stored.deny_count == 1


def test_e2e_e_gated_memory_write_shared_policy_interrupt_resume(tmp_path):
    """(e) gated memory write uses apply_policy, interrupts, and persists always_approve."""
    db_file = str(tmp_path / "permissions.db")
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir()
    connector = MarkdownVaultConnector(str(vault_dir))

    store1 = SqlitePermissionStore(db_file)
    engine1 = LocalPolicy(store=store1)
    proposal = MemoryWriteProposal(
        connector="markdown_vault",
        ref="AI/Decisions/launch_plan.md",
        mode="create",
        content_preview="Launch plan",
        side_effect="write",
    )

    checkpointer = InMemorySaver()
    builder = StateGraph(WorkflowState)

    def write_node(state: WorkflowState) -> dict[str, Any]:
        res = gated_write(connector, proposal, "Launch plan content v1", engine=engine1)
        return {"status": "completed" if res.committed else "failed"}

    builder.add_node("write_node", write_node)
    builder.add_edge(START, "write_node")
    builder.add_edge("write_node", END)
    graph1 = builder.compile(checkpointer=checkpointer)

    config1 = {"configurable": {"thread_id": "vault-thread-1"}}
    paused = graph1.invoke({"task": "write launch plan"}, config=config1)
    assert "__interrupt__" in paused

    # Resume with "always_approve"
    res1 = graph1.invoke(Command(resume="always_approve"), config=config1)
    assert res1["status"] == "completed"
    assert (vault_dir / "AI/Decisions/launch_plan.md").exists()

    # After restart, the *exact same* connector/mode/ref is auto-approved.
    # A second vault is used only to make the same create action executable.
    store2 = SqlitePermissionStore(db_file)
    engine2 = LocalPolicy(store=store2)
    vault_dir2 = tmp_path / "vault-after-restart"
    vault_dir2.mkdir()
    connector2 = MarkdownVaultConnector(str(vault_dir2))
    builder2 = StateGraph(WorkflowState)

    def write_node2(state: WorkflowState) -> dict[str, Any]:
        res = gated_write(
            connector2, proposal, "Launch plan content v2", engine=engine2
        )
        return {"status": "completed" if res.committed else "failed"}

    builder2.add_node("write_node2", write_node2)
    builder2.add_edge(START, "write_node2")
    builder2.add_edge("write_node2", END)
    graph2 = builder2.compile(checkpointer=InMemorySaver())

    config2 = {"configurable": {"thread_id": "vault-thread-2"}}
    res2 = graph2.invoke({"task": "write launch plan update"}, config=config2)
    assert "__interrupt__" not in res2
    assert res2["status"] == "completed"
    assert (vault_dir2 / "AI/Decisions/launch_plan.md").exists()

    # The original create approval must not silently authorize overwriting.
    overwrite = MemoryWriteProposal(
        connector="markdown_vault",
        ref="AI/Decisions/launch_plan.md",
        mode="overwrite",
        content_preview="Launch plan v3",
        side_effect="write",
    )
    builder3 = StateGraph(WorkflowState)

    def write_node3(state: WorkflowState) -> dict[str, Any]:
        res = gated_write(
            connector, overwrite, "Launch plan content v3", engine=engine2
        )
        return {"status": "completed" if res.committed else "failed"}

    builder3.add_node("write_node3", write_node3)
    builder3.add_edge(START, "write_node3")
    builder3.add_edge("write_node3", END)
    graph3 = builder3.compile(checkpointer=InMemorySaver())
    paused_overwrite = graph3.invoke(
        {"task": "overwrite launch plan"},
        config={"configurable": {"thread_id": "vault-thread-3"}},
    )
    assert "__interrupt__" in paused_overwrite


def test_e2e_f_revoke_restores_prompt(tmp_path):
    """(f) revoke restores the approval prompt for matching action."""
    db_file = str(tmp_path / "permissions.db")
    store = SqlitePermissionStore(db_file)
    engine = LocalPolicy(store=store)
    proposal = _memory_action("AI/Decisions/revokable.md")
    perm_key = _permission_key(proposal)

    # Seed rule
    store.upsert(
        PermissionRule(
            permission_key=perm_key, effect="always_approve", tier_at_creation="low"
        )
    )

    graph = _build_test_action_graph(engine, proposal)
    config = {"configurable": {"thread_id": "thread-auto"}}
    res = graph.invoke({"task": "write"}, config=config)
    assert "__interrupt__" not in res
    assert res["status"] == "completed"

    # Revoke
    assert store.delete(perm_key) is True

    # Next run pauses at interrupt again!
    config_after = {"configurable": {"thread_id": "thread-after-revoke"}}
    paused = graph.invoke({"task": "write after revoke"}, config=config_after)
    assert "__interrupt__" in paused


def test_e2e_g_high_tier_never_persisted():
    """(g) high-tier proposal with 'always_approve' executes once but never creates persistent rule."""
    store = InMemoryPermissionStore()
    proposal = ActionProposal(tool="stripe.pay", side_effect="payment")

    # In default taxonomy, payment evaluates to 'deny'. If policy is evaluated to require_approval:
    checkpointer = InMemorySaver()
    builder = StateGraph(WorkflowState)

    def high_tier_node(state: WorkflowState) -> dict[str, Any]:
        # Temporarily force require_approval to test the interrupt resume path
        custom_engine = LocalPolicy(store=store)
        custom_engine.evaluate = lambda p, **kw: MagicMock(
            decision="require_approval", reason="high risk"
        )
        res = apply_policy(
            custom_engine,
            proposal,
            execute_fn=lambda p: ExecutionResult(status="completed"),
        )
        return {"status": res.status}

    builder.add_node("high_node", high_tier_node)
    builder.add_edge(START, "high_node")
    builder.add_edge("high_node", END)
    graph = builder.compile(checkpointer=checkpointer)

    config = {"configurable": {"thread_id": "high-thread"}}
    paused = graph.invoke({"task": "charge card"}, config=config)
    assert "__interrupt__" in paused

    # Resume with always_approve
    res = graph.invoke(Command(resume="always_approve"), config=config)
    assert res["status"] == "completed"

    # Store must have 0 rules
    assert len(store.list()) == 0


def test_high_tier_taxonomy_precedes_existing_store_rule():
    """A stale or manually inserted high-risk grant cannot bypass the taxonomy."""
    store = InMemoryPermissionStore()
    store.upsert(
        PermissionRule(
            permission_key="stripe_pay:payment:legacy-scope",
            effect="always_approve",
            tier_at_creation="high",
        )
    )
    decision = LocalPolicy(store=store).evaluate(
        ActionProposal(tool="stripe.pay", side_effect="payment")
    )
    assert decision.decision == "deny"
    assert decision.policy_id == "default"


def test_unscoped_action_cannot_create_a_remembered_rule(monkeypatch):
    """One HTTP approval cannot become a global URL/payload grant."""
    store = InMemoryPermissionStore()
    execute = MagicMock(return_value=ExecutionResult(status="completed"))
    monkeypatch.setattr("agent_os.policy.interrupt", lambda _: "always_approve")

    result = apply_policy(
        LocalPolicy(store=store),
        ActionProposal(
            tool="http.request",
            side_effect="network",
            arguments={"url": "https://one.example/api"},
        ),
        execute_fn=execute,
    )

    assert result.status == "cancelled"
    assert execute.call_count == 0
    assert store.list() == []


def test_e2e_h_live_store_read_failure_fails_safely():
    """A live store failure falls back to a prompt, never a silent allow."""

    class FailingStore:
        def get(self, permission_key: str) -> PermissionRule | None:
            raise OSError("simulated read failure")

        def upsert(self, rule: PermissionRule) -> None:
            raise OSError("simulated write failure")

        def list(self) -> list[PermissionRule]:
            return []

        def delete(self, permission_key: str) -> bool:
            return False

        def increment_approve(self, permission_key: str) -> None:
            raise OSError("simulated increment failure")

        def increment_deny(self, permission_key: str) -> None:
            raise OSError("simulated increment failure")

    engine = LocalPolicy(store=FailingStore())
    proposal = _memory_action("AI/Decisions/safe.md")

    graph = _build_test_action_graph(engine, proposal)
    config = {"configurable": {"thread_id": "thread-corrupt"}}

    # Safe fallback: does NOT silently allow; it interrupts for approval!
    paused = graph.invoke({"task": "write"}, config=config)
    assert "__interrupt__" in paused


def test_live_store_write_failure_does_not_execute_or_persist(monkeypatch):
    """A failed upsert never turns an always-choice into a one-time allow."""

    class FailingStore:
        def get(self, permission_key: str) -> PermissionRule | None:
            return None

        def upsert(self, rule: PermissionRule) -> None:
            raise OSError("simulated write failure")

        def list(self) -> list[PermissionRule]:
            return []

        def delete(self, permission_key: str) -> bool:
            return False

        def increment_approve(self, permission_key: str) -> None:
            return None

        def increment_deny(self, permission_key: str) -> None:
            return None

    monkeypatch.setattr("agent_os.policy.interrupt", lambda _: "always_approve")
    execute = MagicMock(return_value=ExecutionResult(status="completed"))
    result = apply_policy(
        LocalPolicy(store=FailingStore()),
        _memory_action("AI/Decisions/failing-store.md"),
        execute_fn=execute,
    )
    assert result.status == "cancelled"
    assert execute.call_count == 0


# ==============================================================================
# 8. CLI Commands: agent-os permissions list & revoke
# ==============================================================================


@pytest.mark.anyio
async def test_cli_permissions_list_and_revoke(tmp_path, monkeypatch):
    """Test CLI agent-os permissions list [--json] and agent-os permissions revoke."""
    import io

    db_file = str(tmp_path / "cli_perm.db")
    monkeypatch.setenv("AGENT_OS_PERMISSIONS_DB", db_file)

    buf = io.StringIO()
    console = Console(file=buf)

    # 1. Empty list
    code = await async_main(["permissions", "list"], console=console)
    assert code == 0

    # 2. Seed a rule
    store = SqlitePermissionStore(db_file)
    store.upsert(
        PermissionRule(
            permission_key="file_write:write:foo.txt",
            effect="always_approve",
            tier_at_creation="low",
        )
    )

    # 3. List formatted
    code = await async_main(["permissions", "list"], console=console)
    assert code == 0

    # 4. List json
    code = await async_main(["permissions", "list", "--json"], console=console)
    assert code == 0

    # 5. Revoke nonexistent
    code = await async_main(
        ["permissions", "revoke", "nonexistent:write:*"], console=console
    )
    assert code == 1

    # 6. Revoke existing
    code = await async_main(
        ["permissions", "revoke", "file_write:write:foo.txt"], console=console
    )
    assert code == 0
    assert store.get("file_write:write:foo.txt") is None


# ==============================================================================
# 9. Server API: GET /api/permissions & DELETE /api/permissions/{key}
# ==============================================================================


def test_api_permissions_endpoints(tmp_path, monkeypatch):
    """Test /api/permissions and /api/permissions/{key} endpoints."""
    from fastapi.testclient import TestClient

    from agent_os.server.api import app

    db_file = str(tmp_path / "api_perm.db")
    monkeypatch.setenv("AGENT_OS_PERMISSIONS_DB", db_file)
    monkeypatch.setenv("AGENT_OS_PERMISSIONS_ADMIN_TOKEN", "test-permissions-token")

    store = SqlitePermissionStore(db_file)
    store.upsert(
        PermissionRule(
            permission_key="tool:write:path.txt",
            effect="always_approve",
            tier_at_creation="low",
        )
    )

    client = TestClient(app)
    headers = {"x-admin-token": "test-permissions-token"}

    # GET /api/permissions
    resp = client.get("/api/permissions", headers=headers)
    assert resp.status_code == 200
    rules = resp.json()
    assert len(rules) == 1
    assert rules[0]["permission_key"] == "tool:write:path.txt"

    # DELETE /api/permissions/tool:write:path.txt
    del_resp = client.delete("/api/permissions/tool:write:path.txt", headers=headers)
    assert del_resp.status_code == 200
    assert del_resp.json() == {"status": "ok", "revoked": "tool:write:path.txt"}

    # DELETE again -> 404
    del_resp2 = client.delete("/api/permissions/tool:write:path.txt", headers=headers)
    assert del_resp2.status_code == 404
