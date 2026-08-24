from langgraph.types import interrupt

from agent_os.schemas import ArchitectBrief, PlanArtifact
from agent_os.state import AgentState


def normalize_human_feedback(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("Feedback must be a string")

    v = value.strip().lower()
    if not v:
        raise ValueError("Feedback cannot be empty")

    if v in ("approved", "y", "yes"):
        return "approved"

    if v.startswith("rejected:"):
        reason = value.strip()[9:].strip()
        return f"rejected: {reason or 'no reason provided'}"

    if v in ("rejected", "n", "no"):
        return "rejected: no reason provided"

    raise ValueError(f"Unsupported feedback value: {value}")


def human_gate_node(state: AgentState) -> dict[str, str]:
    plan = state.get("plan")
    if not isinstance(plan, (ArchitectBrief, PlanArtifact)):
        raise ValueError(
            "human_gate_node requires a PlanArtifact or ArchitectBrief in state['plan']"
        )

    prompt = (
        "Review the proposed implementation plan.\n\n"
        f"ArchitectBrief JSON:\n{plan.model_dump_json(indent=2)}\n\n"
        "Reply with 'approved' (or 'y') to proceed, or "
        "'rejected: <reason>' to send back for rework."
    )

    resumed_value = interrupt(prompt)

    normalized = normalize_human_feedback(resumed_value)
    return {"human_feedback": normalized}
