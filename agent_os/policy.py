import logging
import re
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Literal, Protocol

from langgraph.types import interrupt

from agent_os.permission_store import PermissionRule, PermissionRuleStore
from agent_os.schemas import (
    MEMORY_WRITE_MODES,
    ActionProposal,
    ExecutionResult,
    PolicyDecision,
)

logger = logging.getLogger(__name__)

_SIDE_EFFECT_TIER = {
    "read": "trivial",
    "none": "trivial",
    "write": "low",
    "network": "medium",
    "communication": "medium",
    "payment": "high",
    "privileged": "high",
}

# Keep policy-specific choices distinct from the plan-review human gate.  A
# policy interrupt is the only prompt that may accept remembered approvals.
POLICY_APPROVAL_PROMPT_PREFIX = "Policy approval required:"

_POLICY_ENGINE: ContextVar["PolicyEngine | None"] = ContextVar(
    "agent_os_policy_engine",
    default=None,
)
_SAFE_CONNECTOR_NAME = re.compile(r"^[a-z0-9_]+$")


def derive_tier(proposal: ActionProposal) -> str:
    return _SIDE_EFFECT_TIER.get(proposal.side_effect, "medium")


def _permission_connector(value: object) -> str | None:
    """Accept only a collision-free connector name for a learned-rule key."""
    if not isinstance(value, str) or not _SAFE_CONNECTOR_NAME.fullmatch(value):
        return None
    return value


def _permission_ref(value: object) -> str | None:
    """Accept a collision-free memory ref while retaining every component."""
    if not isinstance(value, str):
        return None
    if not value or ":" in value or any(character.isspace() for character in value):
        return None
    return value


def derive_permission_key(proposal: ActionProposal) -> str | None:
    """Return a safely scoped learned-rule key, or ``None`` if none is safe.

    This release intentionally learns only memory writes. Generic tools do not yet
    provide a canonical provider/destination schema, so remembering an approval
    for them could turn one URL, recipient, or file operation into a global
    grant.  Memory grants therefore include connector, mode, and full ref.
    """
    if proposal.tool != "memory.write" or proposal.side_effect != "write":
        return None

    arguments = proposal.arguments or {}
    connector = _permission_connector(proposal.connector)
    mode = arguments.get("mode")
    ref = _permission_ref(arguments.get("ref"))
    if (
        connector is None
        or not isinstance(mode, str)
        or mode not in MEMORY_WRITE_MODES
        or ref is None
    ):
        return None
    return f"memory_write:write:{connector}:{mode}:{ref}"


class PolicyEngine(Protocol):
    def evaluate(
        self,
        proposal: ActionProposal,
        *,
        workspace: Any = None,
        context: Any = None,
    ) -> PolicyDecision: ...


@contextmanager
def policy_scope(engine: PolicyEngine | None) -> Iterator[PolicyEngine | None]:
    """Bind a policy engine to the current async request/thread context."""
    token = _POLICY_ENGINE.set(engine)
    try:
        yield engine
    finally:
        _POLICY_ENGINE.reset(token)


def current_policy_engine() -> PolicyEngine | None:
    """Return the policy bound to this execution context, if any."""
    return _POLICY_ENGINE.get()


def normalize_policy_feedback(value: object) -> str:
    """Normalize feedback for a policy interrupt and reject unknown choices."""
    if not isinstance(value, str):
        raise ValueError("Feedback must be a string")

    raw = value.strip()
    if not raw:
        raise ValueError("Feedback cannot be empty")
    normalized = re.sub(r"[\s-]+", "_", raw.lower())

    if normalized in {"approved", "approve", "y", "yes"}:
        return "approved"
    if normalized in {"session", "approve_session", "session_approve"}:
        return "session"
    if normalized == "always_approve":
        return "always_approve"
    if normalized == "always_deny":
        return "always_deny"
    if normalized.startswith("rejected:"):
        reason = raw.split(":", 1)[1].strip()
        return f"rejected: {reason or 'no reason provided'}"
    if normalized in {"rejected", "reject", "n", "no"}:
        return "rejected: no reason provided"

    raise ValueError(
        "Unsupported policy feedback. Use 'approved', 'rejected: <reason>', "
        "'session', 'always_approve', or 'always_deny'."
    )


class LocalPolicy:
    """Local policy engine enforcing side-effect taxonomy, session grants, and learned rules.

    Modes:
      - 'manual' (default): Low/medium actions prompt for approval; persistence requires an
        explicit user choice ('always_approve', 'always_deny', or 'session').
      - 'smart': Operates as an alias of 'manual' with identical safety boundaries.
      - 'off': Unsafe local-only escape hatch bypassing all checks and auto-allowing all actions
        (including 'payment' and 'privileged'). Intended solely for isolated sandbox testing.
    """

    _session_approved: dict[str, set[str]] = {}
    _session_lock = threading.Lock()

    @classmethod
    def approve_session(cls, session_key: str, permission_key: str) -> None:
        with cls._session_lock:
            if session_key not in cls._session_approved:
                cls._session_approved[session_key] = set()
            cls._session_approved[session_key].add(permission_key)

    @classmethod
    def clear_session(cls, session_key: str) -> None:
        with cls._session_lock:
            cls._session_approved.pop(session_key, None)

    def __init__(
        self,
        rules: dict[str, Any] | None = None,
        *,
        store: PermissionRuleStore | None = None,
        mode: str = "manual",
        session_key: str | None = None,
    ) -> None:
        self.rules = rules or {}
        self.store = store
        if mode not in ("manual", "smart", "off"):
            logger.warning(f"Invalid mode '{mode}', falling back to 'manual'")
            mode = "manual"
        self.mode: Literal["manual", "smart", "off"] = mode  # type: ignore
        self.session_key = session_key

    def with_session(self, session_key: str | None) -> "LocalPolicy":
        """Return a request/thread-scoped LocalPolicy sharing rules and store."""
        return LocalPolicy(
            rules=self.rules,
            store=self.store,
            mode=self.mode,
            session_key=session_key,
        )

    def evaluate(
        self,
        proposal: ActionProposal,
        *,
        workspace: Any = None,
        context: Any = None,
    ) -> PolicyDecision:
        # Step 0: mode == "off" -> allow, policy_id="mode-off" (unsafe escape hatch)
        if self.mode == "off":
            return PolicyDecision(
                decision="allow",
                policy_id="mode-off",
                reason="Policy enforcement disabled (mode=off)",
            )

        # High-tier operations are never eligible for a learned or session
        # grant.  Check the taxonomy *before* looking at a potentially stale,
        # manually edited, or corrupted rule store.
        if derive_tier(proposal) == "high":
            return PolicyDecision(
                decision="deny",
                policy_id="default",
                reason=f"Default taxonomy for {proposal.side_effect}",
            )

        perm_key = derive_permission_key(proposal)
        rule = None

        if self.store is not None and perm_key is not None:
            try:
                rule = self.store.get(perm_key)
            except Exception as e:
                logger.warning(
                    f"Error fetching rule from store for key '{perm_key}': {e}"
                )
                rule = None

        # Step 1: rule.effect == "always_deny" -> deny
        if perm_key is not None and rule is not None and rule.effect == "always_deny":
            if self.store is not None:
                try:
                    self.store.increment_deny(perm_key)
                except Exception as e:
                    logger.warning(
                        f"Error incrementing deny count for key '{perm_key}': {e}"
                    )
            return PolicyDecision(
                decision="deny",
                policy_id="learned",
                reason="always_deny",
            )

        # Step 2: key in _session_approved[sk] -> allow
        if self.session_key is not None and perm_key is not None:
            with self._session_lock:
                approved_set = self._session_approved.get(self.session_key, set())
                if perm_key in approved_set:
                    return PolicyDecision(
                        decision="allow",
                        policy_id="session",
                        reason=f"Session approval for {perm_key}",
                    )

        # Step 3: rule.effect == "always_approve" -> allow
        if (
            perm_key is not None
            and rule is not None
            and rule.effect == "always_approve"
        ):
            if self.store is not None:
                try:
                    self.store.increment_approve(perm_key)
                except Exception as e:
                    logger.warning(
                        f"Error incrementing approve count for key '{perm_key}': {e}"
                    )
            return PolicyDecision(
                decision="allow",
                policy_id="learned",
                reason="always_approve",
            )

        # Step 4: Builtin logs/briefs logic
        if (
            proposal.side_effect == "write"
            and proposal.arguments
            and "ref" in proposal.arguments
        ):
            ref = str(proposal.arguments["ref"])
            mode = str(proposal.arguments.get("mode", ""))
            normalized_ref = ref.lstrip("/")
            is_log = normalized_ref.startswith("AI/Logs/") or normalized_ref.startswith(
                "agentos/logs/"
            )
            if is_log and mode == "append":
                return PolicyDecision(
                    decision="allow",
                    policy_id="builtin-logs",
                    reason="Logs can be appended automatically",
                )
            is_brief = normalized_ref.startswith(
                "AI/Briefs/"
            ) or normalized_ref.startswith("agentos/briefs/")
            if is_brief and mode in ("append", "create"):
                return PolicyDecision(
                    decision="allow",
                    policy_id="builtin-briefs",
                    reason="Briefs can be appended/created automatically",
                )

        # Step 5: side_effect taxonomy
        decision: Literal["allow", "deny", "require_approval"] = "require_approval"
        if proposal.side_effect in ("read", "none"):
            decision = "allow"
        else:  # write, network, communication
            decision = "require_approval"

        return PolicyDecision(
            decision=decision,
            policy_id="default",
            reason=f"Default taxonomy for {proposal.side_effect}",
        )


class CompositePolicyEngine:
    """Additive policy engine that chains built-in policy evaluation with policy plugins.

    Safety Invariant:
    - Built-in policy taxonomy and deny-first checks always run first.
    - Policy plugins can only add restrictions (deny). They can NEVER turn a built-in
      denial into an approval.
    - If built-in policy returns 'deny', the decision is final.
    - If built-in policy returns 'allow' or 'require_approval', any plugin returning
      'deny' overrides the decision to 'deny'.
    - If all plugins return 'allow', the built-in decision stands.
    """

    def __init__(
        self,
        base_policy: PolicyEngine,
        plugins: list[PolicyEngine] | None = None,
    ) -> None:
        self.base_policy = base_policy
        self.plugins: list[PolicyEngine] = plugins or []

    @property
    def mode(self) -> str:
        return getattr(self.base_policy, "mode", "manual")

    @property
    def store(self) -> Any:
        return getattr(self.base_policy, "store", None)

    @property
    def session_key(self) -> str | None:
        return getattr(self.base_policy, "session_key", None)

    @property
    def rules(self) -> dict[str, Any]:
        return getattr(self.base_policy, "rules", {})

    def with_session(self, session_key: str | None) -> "CompositePolicyEngine":
        new_base = (
            self.base_policy.with_session(session_key)
            if hasattr(self.base_policy, "with_session")
            else self.base_policy
        )
        return CompositePolicyEngine(new_base, self.plugins)

    def evaluate(
        self,
        proposal: ActionProposal,
        *,
        workspace: Any = None,
        context: Any = None,
    ) -> PolicyDecision:
        # 1. Preserve the non-bypassable high-tier taxonomy gate even if the
        # base LocalPolicy is configured in its legacy local-only "off" mode.
        # A plugin composition must never become a path around this invariant.
        if proposal.side_effect in ("payment", "privileged"):
            return PolicyDecision(
                decision="deny",
                policy_id="default",
                reason=f"Default taxonomy for {proposal.side_effect}",
            )

        # 2. Built-in policy evaluation always runs first.
        base_decision = self.base_policy.evaluate(
            proposal, workspace=workspace, context=context
        )

        # If built-in policy denies, no plugin can ever override it (especially high-tier payment/privileged)
        if base_decision.decision == "deny":
            return base_decision

        # 3. Evaluate all configured policy plugins.
        for plugin in self.plugins:
            try:
                plugin_decision = plugin.evaluate(
                    proposal, workspace=workspace, context=context
                )
            except Exception as exc:
                # Fail closed on plugin evaluation error
                return PolicyDecision(
                    decision="deny",
                    policy_id="policy-plugin-error",
                    reason=f"Policy plugin evaluation failed: {exc}",
                )

            # A plugin can ONLY add restrictions (deny). An 'allow' from a plugin cannot weaken built-in checks.
            if plugin_decision.decision == "deny":
                return PolicyDecision(
                    decision="deny",
                    policy_id=plugin_decision.policy_id or "policy-plugin-denied",
                    reason=plugin_decision.reason or "Denied by policy plugin",
                )

        # If all plugins allowed, return the base decision
        return base_decision


def apply_policy(
    engine: PolicyEngine,
    proposal: ActionProposal,
    *,
    workspace: Any = None,
    context: Any = None,
    execute_fn: Callable[[ActionProposal], ExecutionResult],
) -> ExecutionResult:
    decision = engine.evaluate(proposal, workspace=workspace, context=context)

    if decision.decision == "allow":
        return execute_fn(proposal)

    elif decision.decision == "require_approval":
        # Interrupt with explicit choices.  The prefix lets the CLI preserve
        # richer feedback here without accepting it for an unrelated plan gate.
        permission_key = derive_permission_key(proposal)
        remembered_scope = (
            f" Remembered choices apply only to `{permission_key}`."
            if permission_key is not None
            else " This action cannot safely be remembered; use one-time approval."
        )
        msg = (
            f"{POLICY_APPROVAL_PROMPT_PREFIX} {proposal.tool} with "
            f"side_effect={proposal.side_effect}. Reason: {decision.reason}\n"
            "Reply 'approved' once, 'rejected: <reason>', 'session', "
            f"'always_approve', or 'always_deny'.{remembered_scope}"
        )
        raw = interrupt(msg)
        try:
            feedback = normalize_policy_feedback(raw)
        except ValueError as e:
            return ExecutionResult(
                status="cancelled", outputs={}, errors=[f"Rejected: {e}"], artifacts=[]
            )

        if feedback == "approved":
            return execute_fn(proposal)
        if feedback.startswith("rejected"):
            return ExecutionResult(
                status="cancelled", outputs={}, errors=[feedback], artifacts=[]
            )

        tier = derive_tier(proposal)
        if tier == "high":
            logger.warning("High-tier proposals cannot create remembered rules")
            if feedback == "always_approve":
                return execute_fn(proposal)
            return ExecutionResult(
                status="cancelled",
                outputs={},
                errors=[
                    "High-risk action rejected; remembered approvals are not allowed"
                ],
                artifacts=[],
            )

        perm_key = derive_permission_key(proposal)
        if perm_key is None:
            return ExecutionResult(
                status="cancelled",
                outputs={},
                errors=[
                    "This action has no safely scoped remembered-permission key. "
                    "Choose 'approved' for one-time approval."
                ],
                artifacts=[],
            )

        if feedback == "session":
            session_key = getattr(engine, "session_key", None)
            if session_key is None:
                return ExecutionResult(
                    status="cancelled",
                    outputs={},
                    errors=[
                        "Session approval is unavailable outside a request/session context. "
                        "Choose 'approved' for one-time approval."
                    ],
                    artifacts=[],
                )
            LocalPolicy.approve_session(session_key, perm_key)
            return execute_fn(proposal)

        effect: Literal["always_approve", "always_deny"]
        effect = "always_approve" if feedback == "always_approve" else "always_deny"
        store = getattr(engine, "store", None)
        if store is None:
            return ExecutionResult(
                status="cancelled",
                outputs={},
                errors=[
                    "Persistent approvals are unavailable because no permission store is configured. "
                    "Choose 'approved' for one-time approval."
                ],
                artifacts=[],
            )
        ws_id = None
        if workspace is not None:
            from agent_os.observations import observation_workspace_id

            ws_id = observation_workspace_id(workspace)
        taught_by = None
        if isinstance(context, dict):
            principal = context.get("principal")
            if principal is not None:
                taught_by = getattr(principal, "id", str(principal))
        if taught_by is None:
            from agent_os.principal import LocalPrincipalResolver

            taught_by = LocalPrincipalResolver().resolve().id

        rule = PermissionRule(
            permission_key=perm_key,
            effect=effect,
            tier_at_creation=tier,  # type: ignore[arg-type]
            workspace_id=ws_id,
            taught_by=taught_by,
        )
        try:
            store.upsert(rule)
        except Exception as e:
            logger.warning(f"Failed to upsert rule for key '{perm_key}': {e}")
            return ExecutionResult(
                status="cancelled",
                outputs={},
                errors=[
                    "Could not save the remembered permission. "
                    "Choose 'approved' for one-time approval."
                ],
                artifacts=[],
            )

        if feedback == "always_approve":
            return execute_fn(proposal)
        return ExecutionResult(
            status="cancelled", outputs={}, errors=["User rejected"], artifacts=[]
        )

    # Deny
    return ExecutionResult(
        status="failed",
        outputs={},
        errors=[decision.reason or "Action denied by policy"],
        artifacts=[],
    )
