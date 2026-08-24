from pathlib import Path
from typing import Annotated, NotRequired

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages
from pydantic import BaseModel, ConfigDict, field_validator
from typing_extensions import TypedDict

from agent_os.schemas import (
    ArchitectBrief,
    ExecutionResult,
    ExecutorReport,
    PlanArtifact,
    ToolExecutionResult,
)
from agent_os.strategies import StrategyHint

PlanField = str | PlanArtifact | ArchitectBrief | None
ResultField = str | ExecutionResult | ExecutorReport | None


class BackendBinding(BaseModel):
    """Effective backend selection persisted with a workflow checkpoint."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    router: str | None
    architect: str | None
    executor: str | None
    profile_name: str | None
    sandbox_root: str

    @field_validator("sandbox_root", mode="before")
    @classmethod
    def normalize_sandbox_root(cls, value: object) -> str:
        text = str(value).strip()
        if not text:
            raise ValueError("sandbox_root must not be empty")
        return str(Path(text).expanduser().resolve())


class AgentState(TypedDict):
    """
    State for the Agent OS graph.
    """

    messages: Annotated[list[AnyMessage], add_messages]
    task: str
    run_id: NotRequired[str | None]
    plan: PlanField
    executor_output: ResultField
    human_feedback: str | None
    hot_context: str | None
    observation_context: NotRequired[str | None]
    strategy_hint: NotRequired[StrategyHint | None]
    conversation_summary: NotRequired[str | None]

    # R7a fields
    tool_result: NotRequired[ToolExecutionResult | None]
    router_escalated: NotRequired[bool]
    backend_binding: NotRequired[BackendBinding | None]
