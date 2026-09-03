from agent_os.checkpoints import CHECKPOINT_MODEL_TYPES
from agent_os.policy import LocalPolicy, apply_policy
from agent_os.schemas import ActionProposal, ExecutionResult, PolicyDecision


def test_policy_taxonomy():
    policy = LocalPolicy()

    # Allow
    assert (
        policy.evaluate(ActionProposal(tool="t", side_effect="read")).decision
        == "allow"
    )
    assert (
        policy.evaluate(ActionProposal(tool="t", side_effect="none")).decision
        == "allow"
    )

    # Require approval
    assert (
        policy.evaluate(ActionProposal(tool="t", side_effect="write")).decision
        == "require_approval"
    )
    assert (
        policy.evaluate(ActionProposal(tool="t", side_effect="network")).decision
        == "require_approval"
    )
    assert (
        policy.evaluate(ActionProposal(tool="t", side_effect="communication")).decision
        == "require_approval"
    )

    # Deny
    assert (
        policy.evaluate(ActionProposal(tool="t", side_effect="payment")).decision
        == "deny"
    )
    assert (
        policy.evaluate(ActionProposal(tool="t", side_effect="privileged")).decision
        == "deny"
    )


def test_policy_logs_briefs_allow():
    policy = LocalPolicy()

    # Logs append
    prop_log = ActionProposal(
        tool="memory.write",
        side_effect="write",
        arguments={"ref": "AI/Logs/2026-08-08.md", "mode": "append"},
    )
    assert policy.evaluate(prop_log).decision == "allow"
    assert policy.evaluate(prop_log).policy_id == "builtin-logs"

    # Briefs create
    prop_brief = ActionProposal(
        tool="memory.write",
        side_effect="write",
        arguments={"ref": "AI/Briefs/x.md", "mode": "create"},
    )
    assert policy.evaluate(prop_brief).decision == "allow"

    # Briefs append
    prop_brief2 = ActionProposal(
        tool="memory.write",
        side_effect="write",
        arguments={"ref": "agentos/briefs/y.md", "mode": "append"},
    )
    assert policy.evaluate(prop_brief2).decision == "allow"

    # Not allow (overwrite)
    prop_overwrite = ActionProposal(
        tool="memory.write",
        side_effect="write",
        arguments={"ref": "AI/Logs/2026-08-08.md", "mode": "overwrite"},
    )
    assert policy.evaluate(prop_overwrite).decision == "require_approval"


def test_apply_policy_allow(monkeypatch):
    called = False

    def mock_exec(p):
        nonlocal called
        called = True
        return ExecutionResult(status="completed", outputs={"x": 1})

    engine = LocalPolicy()
    prop = ActionProposal(tool="t", side_effect="read")
    res = apply_policy(engine, prop, execute_fn=mock_exec)
    assert called
    assert res.status == "completed"


def test_apply_policy_approval_approved(monkeypatch):
    called = False

    def mock_exec(p):
        nonlocal called
        called = True
        return ExecutionResult(status="completed")

    monkeypatch.setattr("agent_os.policy.interrupt", lambda x: "approved")

    engine = LocalPolicy()
    prop = ActionProposal(tool="t", side_effect="write")
    res = apply_policy(engine, prop, execute_fn=mock_exec)
    assert called
    assert res.status == "completed"


def test_apply_policy_approval_rejected(monkeypatch):
    called = False

    def mock_exec(p):
        nonlocal called
        called = True
        return ExecutionResult(status="completed")

    monkeypatch.setattr("agent_os.policy.interrupt", lambda x: "rejected: bad")

    engine = LocalPolicy()
    prop = ActionProposal(tool="t", side_effect="write")
    res = apply_policy(engine, prop, execute_fn=mock_exec)
    assert not called
    assert res.status == "cancelled"


def test_apply_policy_deny(monkeypatch):
    called = False

    def mock_exec(p):
        nonlocal called
        called = True
        return ExecutionResult(status="completed")

    engine = LocalPolicy()
    prop = ActionProposal(tool="t", side_effect="payment")
    res = apply_policy(engine, prop, execute_fn=mock_exec)
    assert not called
    assert res.status == "failed"


def test_action_proposal_7_levels():
    # 2 level moi hop le
    a = ActionProposal(tool="t", side_effect="communication")
    b = ActionProposal(tool="t", side_effect="privileged")
    assert a.side_effect == "communication"
    assert b.side_effect == "privileged"

    # Checkpoint cu van valid (json payload parse duoc)
    raw = '{"tool": "t", "side_effect": "read"}'
    c = ActionProposal.model_validate_json(raw)
    assert c.side_effect == "read"


def test_policy_decision_in_allowlist():
    assert PolicyDecision in CHECKPOINT_MODEL_TYPES
