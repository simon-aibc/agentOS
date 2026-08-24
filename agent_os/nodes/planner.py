import asyncio

from langchain_core.runnables import RunnableLambda

from agent_os.context import load_injected_context
from agent_os.schemas import ArchitectBrief, PlanArtifact
from agent_os.state import AgentState


def _sync_planner_logic(state: AgentState) -> dict:
    result: dict[str, object] = {}
    if not isinstance(state.get("plan"), (ArchitectBrief, PlanArtifact)):
        result["plan"] = state["task"]

    try:
        from agent_os.server.runtime import composed_workspace

        runtime = composed_workspace()
        if runtime is not None and getattr(runtime, "context_providers", None):
            max_chars = int(runtime.workspace.context.get("max_chars", 8000))
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None

            if loop is None or not loop.is_running():
                injected = asyncio.run(
                    load_injected_context(
                        runtime.context_providers,
                        state["task"],
                        max_chars=max_chars,
                        workspace=runtime.workspace,
                        policy=getattr(runtime, "policy", None),
                        run_id=state.get("run_id"),
                    )
                )
                if injected:
                    existing_hot = state.get("hot_context") or ""
                    if existing_hot:
                        combined = f"{existing_hot}\n\n{injected}"
                    else:
                        combined = injected
                    result["hot_context"] = combined[:max_chars]
    except Exception:
        pass

    return result


async def _async_planner_logic(state: AgentState) -> dict:
    result: dict[str, object] = {}
    if not isinstance(state.get("plan"), (ArchitectBrief, PlanArtifact)):
        result["plan"] = state["task"]

    try:
        from agent_os.server.runtime import composed_workspace

        runtime = composed_workspace()
        if runtime is not None and getattr(runtime, "context_providers", None):
            max_chars = int(runtime.workspace.context.get("max_chars", 8000))
            injected = await load_injected_context(
                runtime.context_providers,
                state["task"],
                max_chars=max_chars,
                workspace=runtime.workspace,
                policy=getattr(runtime, "policy", None),
                run_id=state.get("run_id"),
            )
            if injected:
                existing_hot = state.get("hot_context") or ""
                if existing_hot:
                    combined = f"{existing_hot}\n\n{injected}"
                else:
                    combined = injected
                result["hot_context"] = combined[:max_chars]
    except Exception:
        pass

    return result


planner_node = RunnableLambda(_sync_planner_logic, afunc=_async_planner_logic)
