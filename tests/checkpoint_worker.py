import json
import os
import sys
from pathlib import Path
from threading import Event

from langgraph.types import Command

from agent_os.graph import build_graph
from agent_os.routing import build_runtime_config
from agent_os.schemas import ArchitectBrief, ExecutorReport
from agent_os.state import AgentState


def fake_architect_node(state: AgentState) -> dict[str, ArchitectBrief]:
    return {
        "plan": ArchitectBrief(
            files=["restart_demo.py"],
            changes=["prove restart-safe execution"],
            verify_cmd="pytest",
        )
    }


def fake_executor_node(state: AgentState) -> dict[str, ExecutorReport]:
    return {
        "executor_output": ExecutorReport(
            diff="restart-safe diff",
            verify_output="cross-process verification passed",
            success=True,
        )
    }


def pause_workflow(thread_id: str, status_path: Path) -> None:
    graph = build_graph(
        architect_node_impl=fake_architect_node,
        executor_node_impl=fake_executor_node,
        tool_dispatcher_node_impl=lambda state: Command(
            update={"router_escalated": True},
            goto="supervisor",
        ),
    )
    config = build_runtime_config(thread_id)
    initial_state: AgentState = {
        "messages": [],
        "task": "prove workflow survives restart",
        "plan": None,
        "executor_output": None,
        "human_feedback": None,
        "hot_context": None,
    }

    result = graph.invoke(initial_state, config=config)
    snapshot = graph.get_state(config)
    temporary_status_path = status_path.with_suffix(".tmp")
    temporary_status_path.write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "interrupted": "__interrupt__" in result,
                "next": list(snapshot.next),
                "task": snapshot.values["task"],
            }
        ),
        encoding="utf-8",
    )
    temporary_status_path.replace(status_path)
    Event().wait()


def resume_workflow(thread_id: str) -> None:
    graph = build_graph(
        architect_node_impl=fake_architect_node,
        executor_node_impl=fake_executor_node,
        tool_dispatcher_node_impl=lambda state: Command(
            update={"router_escalated": True},
            goto="supervisor",
        ),
    )
    config = build_runtime_config(thread_id)
    before_resume = graph.get_state(config)
    result = graph.invoke(Command(resume="approved"), config=config)
    report = result["executor_output"]
    after_resume = graph.get_state(config)
    print(
        json.dumps(
            {
                "pid": os.getpid(),
                "before_next": list(before_resume.next),
                "task": result["task"],
                "human_feedback": result["human_feedback"],
                "success": report.success,
                "verify_output": report.verify_output,
                "after_next": list(after_resume.next),
            }
        ),
        flush=True,
    )


def main() -> None:
    if len(sys.argv) < 3 or sys.argv[1] not in {"pause", "resume"}:
        raise SystemExit(
            "usage: checkpoint_worker.py pause <thread_id> <status_path> | "
            "resume <thread_id>"
        )

    action, thread_id = sys.argv[1:3]
    if action == "pause":
        if len(sys.argv) != 4:
            raise SystemExit("pause requires a status_path")
        pause_workflow(thread_id, Path(sys.argv[3]))
    else:
        if len(sys.argv) != 3:
            raise SystemExit("resume accepts only a thread_id")
        resume_workflow(thread_id)


if __name__ == "__main__":
    main()
