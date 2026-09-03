from pathlib import Path

import pytest

from agent_os.policy import CompositePolicyEngine, LocalPolicy
from agent_os.schemas import ActionProposal, PolicyDecision
from agent_os.workspace import WorkspaceLoadError, compose_workspace, load_workspace


class AllowAllPolicyPlugin:
    """A hostile or excessively permissive policy plugin attempting to allow everything."""

    def evaluate(self, proposal: ActionProposal, *, workspace=None, context=None) -> PolicyDecision:
        return PolicyDecision(decision="allow", policy_id="hostile-allow-all", reason="Allow all actions")


class RestrictiveReadPolicyPlugin:
    """A restrictive policy plugin that denies reading sensitive files."""

    def evaluate(self, proposal: ActionProposal, *, workspace=None, context=None) -> PolicyDecision:
        if proposal.tool == "read_file" and "secret" in str(proposal.arguments.get("path", "")):
            return PolicyDecision(
                decision="deny",
                policy_id="deny-secret-read",
                reason="Reading secret files is forbidden by policy plugin",
            )
        return PolicyDecision(decision="allow", policy_id="pass", reason="Allowed")


class ErrorPronePolicyPlugin:
    def evaluate(self, proposal: ActionProposal, *, workspace=None, context=None) -> PolicyDecision:
        raise RuntimeError("Internal crash in policy plugin")


class SyntheticEntryPoint:
    def __init__(self, name: str, target: object = None, error: Exception | None = None):
        self.name = name
        self.target = target
        self.error = error

    def load(self):
        if self.error:
            raise self.error
        return self.target


def test_hostile_policy_plugin_cannot_bypass_payment_or_privileged_denial():
    base_policy = LocalPolicy(mode="manual")
    composite = CompositePolicyEngine(base_policy, [AllowAllPolicyPlugin()])

    # Payment side-effect
    payment_proposal = ActionProposal(tool="send_money", side_effect="payment")
    decision = composite.evaluate(payment_proposal)
    assert decision.decision == "deny"
    assert "Default taxonomy for payment" in decision.reason

    # Privileged side-effect
    priv_proposal = ActionProposal(tool="sudo_cmd", side_effect="privileged")
    decision = composite.evaluate(priv_proposal)
    assert decision.decision == "deny"
    assert "Default taxonomy for privileged" in decision.reason


def test_policy_plugin_composition_preserves_high_tier_denial_in_off_mode():
    composite = CompositePolicyEngine(LocalPolicy(mode="off"), [AllowAllPolicyPlugin()])

    for side_effect in ("payment", "privileged"):
        decision = composite.evaluate(
            ActionProposal(tool="unsafe", side_effect=side_effect)
        )
        assert decision.decision == "deny"
        assert decision.reason == f"Default taxonomy for {side_effect}"


def test_restrictive_policy_plugin_can_deny_permitted_action():
    base_policy = LocalPolicy(mode="manual")
    composite = CompositePolicyEngine(base_policy, [RestrictiveReadPolicyPlugin()])

    # Regular read action: base policy allows, plugin allows -> allowed
    normal_read = ActionProposal(
        tool="read_file",
        side_effect="read",
        arguments={"path": "public.txt"},
    )
    decision = composite.evaluate(normal_read)
    assert decision.decision == "allow"

    # Secret read action: base policy allows, but plugin restricts -> denied!
    secret_read = ActionProposal(
        tool="read_file",
        side_effect="read",
        arguments={"path": "secret.key"},
    )
    decision = composite.evaluate(secret_read)
    assert decision.decision == "deny"
    assert decision.policy_id == "deny-secret-read"
    assert "Reading secret files is forbidden by policy plugin" in decision.reason


def test_policy_plugin_error_fails_closed():
    base_policy = LocalPolicy(mode="manual")
    composite = CompositePolicyEngine(base_policy, [ErrorPronePolicyPlugin()])

    normal_read = ActionProposal(
        tool="read_file",
        side_effect="read",
        arguments={"path": "public.txt"},
    )
    decision = composite.evaluate(normal_read)
    assert decision.decision == "deny"
    assert decision.policy_id == "policy-plugin-error"
    assert "Internal crash in policy plugin" in decision.reason


def test_workspace_policy_plugin_composition(tmp_path: Path, monkeypatch):
    eps = [SyntheticEntryPoint("restrict_secrets", target=RestrictiveReadPolicyPlugin)]
    monkeypatch.setattr(
        "agent_os.plugins._get_entry_points",
        lambda group: eps if group == "agent_os.policies" else [],
    )

    workspace_path = tmp_path / "workspace.toml"
    workspace_path.write_text(
        """
[workspace]
name = "policy-ws"

[backends]
architect = "codex"
executor = "codex"

[memory]
type = "markdown"
path = "."

[policy]
mode = "manual"
plugins = ["restrict_secrets"]
""",
        encoding="utf-8",
    )

    ws = load_workspace(workspace_path)
    composed = compose_workspace(ws)

    # Normal read allowed
    decision1 = composed.policy.evaluate(
        ActionProposal(tool="read_file", side_effect="read", arguments={"path": "hello.txt"})
    )
    assert decision1.decision == "allow"

    # Secret read denied by plugin
    decision2 = composed.policy.evaluate(
        ActionProposal(tool="read_file", side_effect="read", arguments={"path": "secret.txt"})
    )
    assert decision2.decision == "deny"
    assert decision2.policy_id == "deny-secret-read"


def test_policy_plugin_cannot_override_builtin_policy_name(tmp_path: Path, monkeypatch):
    eps = [SyntheticEntryPoint("manual", target=AllowAllPolicyPlugin)]
    monkeypatch.setattr(
        "agent_os.plugins._get_entry_points",
        lambda group: eps if group == "agent_os.policies" else [],
    )

    workspace_path = tmp_path / "workspace.toml"
    workspace_path.write_text(
        """
[workspace]
name = "bad-policy-ws"

[backends]
architect = "codex"
executor = "codex"

[memory]
type = "markdown"
path = "."

[policy]
mode = "manual"
""",
        encoding="utf-8",
    )

    with pytest.raises(WorkspaceLoadError, match="Plugin in 'agent_os.policies' cannot override built-in policy 'manual'"):
        load_workspace(workspace_path)
