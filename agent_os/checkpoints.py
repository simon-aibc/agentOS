import atexit
import os
import sqlite3
from pathlib import Path

from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite import SqliteSaver

from agent_os.schemas import (
    ActionProposal,
    ArchitectBrief,
    BashResult,
    CodingPlan,
    CodingResult,
    EditFileResult,
    ExecutionResult,
    ExecutorReport,
    GrepMatch,
    GrepResult,
    MemoryWriteProposal,
    PlanArtifact,
    PolicyDecision,
    ReadFileResult,
    RouterDecision,
    ToolExecutionResult,
)
from agent_os.state import BackendBinding
from agent_os.strategies import StrategyHint

CHECKPOINT_DB_ENV = "AGENT_OS_CHECKPOINTS_DB"
DEFAULT_CHECKPOINT_DB = "./checkpoints.db"
CHECKPOINT_MODEL_TYPES = (
    ArchitectBrief,
    BashResult,
    EditFileResult,
    ExecutorReport,
    GrepMatch,
    GrepResult,
    ReadFileResult,
    RouterDecision,
    ToolExecutionResult,
    BackendBinding,
    PlanArtifact,
    ExecutionResult,
    CodingPlan,
    CodingResult,
    ActionProposal,
    MemoryWriteProposal,
    PolicyDecision,
    StrategyHint,
)


def get_checkpoint_serializer() -> JsonPlusSerializer:
    """Allow only the application models persisted in AgentState."""
    return JsonPlusSerializer(allowed_msgpack_modules=CHECKPOINT_MODEL_TYPES)


def get_default_checkpointer(
    database_path: str | Path | None = None,
) -> SqliteSaver:
    """Create a SQLite checkpointer using an explicit or configured path."""
    configured_path = (
        str(database_path)
        if database_path is not None
        else os.getenv(CHECKPOINT_DB_ENV, DEFAULT_CHECKPOINT_DB)
    )
    if not configured_path.strip():
        raise ValueError(f"{CHECKPOINT_DB_ENV} must not be empty")

    path = Path(configured_path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, check_same_thread=False)
    atexit.register(connection.close)
    return SqliteSaver(connection, serde=get_checkpoint_serializer())
