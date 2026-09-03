from typing import Any, Literal

from agent_os.connectors import MemoryWriteResult, WritableMemory
from agent_os.policy import (
    LocalPolicy,
    PolicyEngine,
    apply_policy,
    current_policy_engine,
)
from agent_os.schemas import (
    MEMORY_WRITE_MODES,
    ActionProposal,
    ExecutionResult,
    MemoryWriteProposal,
)


def evaluate_write_policy(
    ref: str,
    mode: str,
    *,
    connector: str = "memory",
    engine: PolicyEngine | None = None,
    store: Any = None,
    policy_mode: str = "manual",
    session_key: str | None = None,
) -> Literal["auto", "gate"]:
    """Evaluate whether a memory write proposal is auto-approved or requires gating."""
    proposal = ActionProposal(
        tool="memory.write",
        connector=connector,
        arguments={"ref": ref, "mode": mode},
        side_effect="write",
    )
    policy_engine = (
        engine
        or current_policy_engine()
        or LocalPolicy(
            store=store,
            mode=policy_mode,
            session_key=session_key,
        )
    )
    decision = policy_engine.evaluate(proposal)
    return "auto" if decision.decision == "allow" else "gate"


def gated_write(
    connector: WritableMemory,
    proposal: MemoryWriteProposal,
    content: str,
    frontmatter: dict[str, Any] | None = None,
    *,
    engine: PolicyEngine | None = None,
    store: Any = None,
    policy_mode: str = "manual",
    session_key: str | None = None,
) -> MemoryWriteResult:
    """Execute a gated write to memory through the shared PolicyEngine gating path."""
    if proposal.mode not in MEMORY_WRITE_MODES:
        raise ValueError(f"Unsupported memory write mode: {proposal.mode!r}")
    supported_modes = getattr(connector, "supported_write_modes", MEMORY_WRITE_MODES)
    if (
        not isinstance(supported_modes, (set, frozenset, list, tuple))
        or proposal.mode not in supported_modes
    ):
        return MemoryWriteResult(
            ref=proposal.ref,
            mode=proposal.mode,
            bytes_written=None,
            committed=False,
            status="failed",
            error=(
                f"Memory connector '{getattr(connector, 'name', 'unknown')}' "
                f"does not safely support mode '{proposal.mode}'."
            ),
        )
    # Scope remembered permission to the connector that will actually execute
    # the operation rather than trusting a caller-provided display field.
    actual_connector = getattr(connector, "name", proposal.connector)
    action = ActionProposal(
        tool="memory.write",
        connector=str(actual_connector),
        arguments={"ref": proposal.ref, "mode": proposal.mode},
        side_effect="write",
    )

    policy_engine = (
        engine
        or current_policy_engine()
        or LocalPolicy(
            store=store,
            mode=policy_mode,
            session_key=session_key,
        )
    )
    write_result: MemoryWriteResult | None = None

    def _execute(prop: ActionProposal) -> ExecutionResult:
        nonlocal write_result
        # Preserve the connector contract: an execution failure is distinct
        # from a denied/cancelled approval and must remain observable to callers.
        write_result = connector.write_note(
            proposal.ref, content, frontmatter=frontmatter, mode=proposal.mode
        )
        return ExecutionResult(
            status="completed",
            outputs={"result": write_result},
            artifacts=[],
            errors=[],
        )

    exec_res = apply_policy(policy_engine, action, execute_fn=_execute)

    if exec_res.status == "completed" and write_result is not None:
        return write_result

    # ``apply_policy`` represents a denial, user cancellation, or an inability
    # to persist an opted-in permission as an ExecutionResult.  Do not flatten
    # that information into an anonymous ``committed=False``: callers such as
    # the brief runtime must be able to report the actual outcome.
    error = "; ".join(str(item) for item in exec_res.errors if str(item).strip())
    if not error:
        error = f"Memory write {exec_res.status} before it reached the connector."

    return MemoryWriteResult(
        ref=proposal.ref,
        mode=proposal.mode,
        bytes_written=None,
        committed=False,
        status=exec_res.status,
        error=error,
    )
