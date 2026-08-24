from typing import Any

from agent_os.api import ExecutionResult


class PrivateProfileContext:
    name = "private_profile"

    def provide(self, task: str, **_kwargs: Any) -> str:
        return f"Private application context is available for: {task[:80]}"


class PrivateRecordsConnector:
    name = "private_records"

    def capabilities(self) -> dict[str, Any]:
        return {"actions": ["lookup"]}

    def describe_side_effect(self, action: str) -> str:
        return "read"

    def invoke(self, action: str, args: dict[str, Any]) -> ExecutionResult:
        return ExecutionResult(status="completed", outputs={"action": action, "args": args})
