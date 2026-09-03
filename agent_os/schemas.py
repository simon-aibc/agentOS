from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

MemoryWriteMode = Literal["create", "append", "overwrite"]
MEMORY_WRITE_MODES = frozenset({"append", "create", "overwrite"})


class ReadFileResult(BaseModel):
    path: str
    content: str


class GrepMatch(BaseModel):
    path: str
    line: int
    text: str


class GrepResult(BaseModel):
    matches: list[GrepMatch]


class EditFileResult(BaseModel):
    path: str
    bytes_written: int


class BashResult(BaseModel):
    args: list[str]
    returncode: int | None
    stdout: str
    stderr: str
    timed_out: bool
    truncated: bool = False


class RouterDecision(BaseModel):
    """Output of the router LLM classifying the user's intent."""

    tool: str | None
    confidence: float = Field(..., ge=0.0, le=1.0)
    arguments: dict[str, object] = Field(default_factory=dict)


class ToolExecutionResult(BaseModel):
    """Output of executing a native tool."""

    tool: str
    output: str
    success: bool


SideEffect = Literal[
    "none", "read", "write", "network", "payment", "communication", "privileged"
]
SUPPORTED_SIDE_EFFECTS = frozenset(
    {"none", "read", "write", "network", "payment", "communication", "privileged"}
)


class ActionProposal(BaseModel):
    tool: str
    arguments: dict[str, object] = Field(default_factory=dict)
    reason: str = ""
    side_effect: SideEffect = "none"
    connector: str | None = None


class MemoryWriteProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    connector: str
    ref: str
    mode: MemoryWriteMode
    content_preview: str
    side_effect: str


class PlanArtifact(BaseModel):
    summary: str = ""
    steps: list[str] = Field(default_factory=list)
    proposed_actions: list[ActionProposal] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list)
    metadata: dict[str, object] = Field(default_factory=dict)


class ExecutionResult(BaseModel):
    status: Literal["completed", "failed", "cancelled", "waiting"]
    outputs: dict[str, object] = Field(default_factory=dict)
    artifacts: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    usage: dict[str, float] = Field(default_factory=dict)
    self_check: dict = Field(default_factory=dict)

    @property
    def success(self) -> bool:
        return self.status == "completed"


class CodingPlan(PlanArtifact):
    files: list[str]
    changes: list[str]
    verify_cmd: str


class CodingResult(ExecutionResult):
    diff: str = ""
    verify_output: str = ""


ArchitectBrief = CodingPlan


class ExecutorReport(CodingResult):
    def __init__(self, **data):
        if "success" in data and "status" not in data:
            data["status"] = "completed" if data.pop("success") else "failed"
        super().__init__(**data)


class PolicyDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: Literal["allow", "deny", "require_approval"]
    policy_id: str
    reason: str = ""
    approver_roles: list[str] = Field(default_factory=list)
    transformed_arguments: dict[str, object] | None = None
