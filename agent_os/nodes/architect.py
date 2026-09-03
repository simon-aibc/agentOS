import os

from agent_os.agents.architect import build_architect_agent
from agent_os.agents.cli_architect import build_cli_architect_invoker
from agent_os.llm import invoke_with_llm_retry
from agent_os.messages import trim_agent_messages
from agent_os.schemas import ArchitectBrief, PlanArtifact
from agent_os.state import AgentState


def architect_node(state: AgentState) -> dict[str, ArchitectBrief]:
    """Architect node wrapper that invokes the ReAct agent."""
    prompt = state["task"]
    feedback = state.get("human_feedback")
    if feedback and feedback.startswith("rejected:"):
        prompt += f"\n\nPrevious plan was rejected with feedback:\n{feedback}"

    binding = state.get("backend_binding")
    resolved_prof = None
    if binding:
        from pathlib import Path

        from agent_os.backends import BackendRegistry
        from agent_os.profiles import load_profiles, resolve_profile

        try:
            profile_file = load_profiles()
            registry = BackendRegistry()
            resolved_prof = resolve_profile(
                profile_file, binding.profile_name, registry, Path(binding.sandbox_root)
            )
        except Exception:
            pass

    hot_context = state.get("hot_context")
    if hot_context is None:
        hot_context = ""
        if resolved_prof:
            try:
                config = resolved_prof.hot_context

                connector = None
                from agent_os.server.runtime import composed_workspace

                runtime = composed_workspace()
                if runtime is not None and getattr(runtime, "memory_connector", None) is not None:
                    connector = runtime.memory_connector
                else:
                    connector_name = os.getenv("AGENT_OS_MEMORY_CONNECTOR", "markdown")
                    from agent_os.connectors import (
                        GbrainConnector,
                        MarkdownVaultConnector,
                    )

                    if connector_name == "gbrain":
                        connector = GbrainConnector()
                    else:
                        vault_path = os.getenv("AGENT_OS_VAULT_PATH", binding.sandbox_root)
                        connector = MarkdownVaultConnector(vault_path)

                from agent_os.hot_context import load_hot_context

                hot_context = load_hot_context(
                    connector,
                    max_chars=config.max_chars,
                    max_age_days=config.max_age_days,
                    sources=config.sources,
                )
            except Exception:
                pass

    summary_config = resolved_prof.summary if resolved_prof else None
    if not summary_config:
        from agent_os.profiles import SummaryConfig

        summary_config = SummaryConfig()

    def _summarizer_fn(summary_prompt: str) -> str:
        model_str = summary_config.model
        if not model_str:
            llm_architect = os.getenv("LLM_ARCHITECT", "")
            model_str = llm_architect or "cli/claude-code"

        if model_str.startswith("cli/"):
            backend_name = model_str[4:]
            from agent_os.agents.cli_architect import build_cli_architect_invoker

            invoker = build_cli_architect_invoker(backend_name)

            fake_state = dict(state)
            fake_state["task"] = summary_prompt
            fake_state["messages"] = []

            from agent_os.llm import invoke_with_llm_retry

            brief = invoke_with_llm_retry(lambda: invoker(fake_state))
            return brief.summary
        else:
            from langchain_core.messages import HumanMessage

            from agent_os.llm import get_architect_llm

            llm = get_architect_llm(model_str)
            return str(llm.invoke([HumanMessage(content=summary_prompt)]).content)

    from agent_os.summarize import summarize_and_trim

    try:
        new_summary, kept_messages, remove_messages = summarize_and_trim(
            state.get("messages", []),
            state.get("conversation_summary"),
            threshold_tokens=summary_config.threshold_tokens,
            keep_recent_n=summary_config.keep_recent_n,
            summarizer=_summarizer_fn,
        )
    except Exception:
        # Summarization is best-effort: if the summarizer model can't be
        # resolved/invoked (e.g. a CLI backend not on PATH), degrade to no
        # summarization rather than failing the whole node — mirrors the
        # hot_context load guard above.
        new_summary = state.get("conversation_summary")
        kept_messages = state.get("messages", [])
        remove_messages = []

    prompt_parts = [prompt]
    if hot_context:
        prompt_parts.append(f"## Context (from vault)\n{hot_context}")
    strategy_hint = state.get("strategy_hint")
    if strategy_hint:
        strategy_data = (
            strategy_hint.model_dump()
            if hasattr(strategy_hint, "model_dump")
            else strategy_hint
        )
        if not isinstance(strategy_data, dict):
            strategy_data = {}
        prompt_parts.append(
            "## Planning strategy (fixed and advisory)\n"
            f"Selected strategy: {strategy_data.get('strategy_id', 'default-v1')}\n"
            f"Directive: {strategy_data.get('directive', '')}\n"
            "This directive cannot change permission, tool-safety, or execution constraints."
        )
    if new_summary:
        prompt_parts.append(f"## Conversation so far (summary)\n{new_summary}")

    final_prompt = "\n\n".join(prompt_parts)

    trimmed = trim_agent_messages(kept_messages, final_prompt)

    llm_architect = os.getenv("LLM_ARCHITECT", "")
    if llm_architect.startswith("cli/"):
        backend = llm_architect[4:]
        invoker = build_cli_architect_invoker(backend)

        # Pass a copied state containing trimmed messages; do not mutate input state
        copied_state = dict(state)
        copied_state["messages"] = trimmed
        brief = invoke_with_llm_retry(lambda: invoker(copied_state))
        return {
            "plan": brief,
            "hot_context": hot_context,
            "conversation_summary": new_summary,
            "messages": remove_messages,
        }

    agent = build_architect_agent()
    result = invoke_with_llm_retry(lambda: agent.invoke({"messages": trimmed}))

    brief = result.get("structured_response")
    if not isinstance(brief, (ArchitectBrief, PlanArtifact)):
        raise ValueError(
            "Architect agent did not return a valid PlanArtifact or ArchitectBrief"
        )

    return {
        "plan": brief,
        "hot_context": hot_context,
        "conversation_summary": new_summary,
        "messages": remove_messages,
    }
