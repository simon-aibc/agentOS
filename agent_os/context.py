"""Pre-planner context provider contracts and runtime injection."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from agent_os.principal import LocalPrincipalResolver, Principal
from agent_os.schemas import ActionProposal

logger = logging.getLogger(__name__)

DEFAULT_CONTEXT_TIMEOUT_SECONDS = 5.0


def _principal_for_run(run_id: str | None) -> Principal | None:
    """Recover the durable run actor without re-reading request credentials."""
    if not run_id:
        return None
    try:
        from agent_os import runs

        run = runs.get_run(run_id)
        if not run or not run.get("created_by"):
            return None
        principal_id = str(run["created_by"])
        principal_kind = str(run.get("created_by_kind") or "system")
        return Principal(
            id=principal_id,
            kind=principal_kind,
            display=principal_id,
        )
    except Exception:
        return None


class ContextBlock(BaseModel):
    """A discrete block of context injected before planning."""

    source: str
    content: str
    title: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


@runtime_checkable
class ContextProvider(Protocol):
    """Extension protocol for providing contextual information prior to planning."""

    @property
    def name(self) -> str: ...

    def provide(
        self,
        task: str,
        *,
        budget_chars: int,
        principal: Principal,
        workspace_id: str,
    ) -> list[ContextBlock] | ContextBlock | str: ...


async def load_injected_context(
    providers: list[ContextProvider],
    task: str,
    *,
    max_chars: int = 8000,
    workspace: Any = None,
    policy: Any = None,
    run_id: str | None = None,
    principal: Principal | None = None,
    workspace_id: str | None = None,
    timeout_seconds: float = DEFAULT_CONTEXT_TIMEOUT_SECONDS,
) -> str:
    """Load and format injected context from configured providers with bounds and safety checks."""
    if not providers or max_chars <= 0:
        return ""

    if principal is None:
        principal = _principal_for_run(run_id)
    if principal is None:
        try:
            principal = LocalPrincipalResolver().resolve()
        except Exception:
            principal = Principal(
                id="local:unknown", kind="human", display="operator"
            )

    if workspace_id is None:
        if workspace is not None:
            from agent_os.observations import observation_workspace_id

            workspace_id = observation_workspace_id(workspace)
        if not workspace_id:
            workspace_id = "default"

    content_parts: list[str] = []
    current_chars = 0

    for provider in providers:
        remaining_budget = max_chars - current_chars - (1 if current_chars > 0 else 0)
        if remaining_budget <= 0:
            break

        provider_name = getattr(provider, "name", "unknown")

        # 1. Policy check: context provision is a read operation
        if policy is not None and hasattr(policy, "evaluate"):
            proposal = ActionProposal(
                tool=f"context_provider:{provider_name}",
                side_effect="read",
                arguments={"task": task, "provider": provider_name},
            )
            try:
                decision = policy.evaluate(proposal, workspace=workspace)
                if getattr(decision, "decision", "") == "deny":
                    logger.warning(
                        f"Context provider '{provider_name}' was denied by policy: "
                        f"{getattr(decision, 'reason', 'Policy denied')}"
                    )
                    continue
            except Exception as exc:
                logger.warning(
                    f"Policy check failed for context provider '{provider_name}': {exc}"
                )
                continue

        # 2. Async offload with prompt timeout (fail-soft, non-blocking)
        try:
            coro = asyncio.to_thread(
                provider.provide,
                task,
                budget_chars=remaining_budget,
                principal=principal,
                workspace_id=workspace_id,
            )
            raw_result = await asyncio.wait_for(coro, timeout=timeout_seconds)
        except TimeoutError:
            logger.warning(
                f"Context provider '{provider_name}' timed out after {timeout_seconds}s."
            )
            continue
        except Exception as exc:
            logger.warning(
                f"Context provider '{provider_name}' raised an exception: {exc}"
            )
            continue

        # 3. Normalize raw result into list of ContextBlock
        blocks: list[ContextBlock] = []
        if isinstance(raw_result, str):
            if raw_result.strip():
                blocks.append(
                    ContextBlock(source=provider_name, content=raw_result.strip())
                )
        elif isinstance(raw_result, ContextBlock):
            blocks.append(raw_result)
        elif isinstance(raw_result, (list, tuple)):
            for item in raw_result:
                if isinstance(item, ContextBlock):
                    blocks.append(item)
                elif isinstance(item, dict):
                    src = str(item.get("source") or provider_name)
                    cnt = str(item.get("content") or "")
                    title = item.get("title")
                    meta = item.get("metadata") or {}
                    if cnt.strip():
                        blocks.append(
                            ContextBlock(
                                source=src,
                                content=cnt.strip(),
                                title=str(title) if title else None,
                                metadata=meta if isinstance(meta, dict) else {},
                            )
                        )
                elif isinstance(item, str) and item.strip():
                    blocks.append(
                        ContextBlock(source=provider_name, content=item.strip())
                    )

        # 4. Strict budget bounding and provenance recording
        for block in blocks:
            if not block.content:
                continue

            header = f"--- BEGIN CONTEXT [{block.source}] ---"
            if block.title:
                header = f"--- BEGIN CONTEXT [{block.source}: {block.title}] ---"
            footer = f"--- END CONTEXT [{block.source}] ---"
            join_len = 1 if current_chars > 0 else 0
            remaining = max_chars - current_chars - join_len
            if remaining <= 0:
                break

            # Preserve framing even when a provider overproduces: downstream
            # consumers must never receive a half-open context block.
            envelope_len = len(header) + len(footer) + 3
            if remaining < envelope_len and block.title:
                # The title is optional display detail; keep the source and
                # complete delimiter pair when the caller grants a tiny budget.
                header = f"--- BEGIN CONTEXT [{block.source}] ---"
                envelope_len = len(header) + len(footer) + 3
            if remaining < envelope_len:
                break
            content_budget = remaining - envelope_len
            formatted = f"{header}\n{block.content[:content_budget]}\n{footer}\n"

            if not formatted:
                break

            content_parts.append(formatted)
            current_chars += len(formatted) + join_len

            # 5. Record only fixed provenance fields. Provider metadata is
            # untrusted and may itself contain sensitive values.
            if run_id:
                try:
                    from agent_os import runs

                    runs.append_event(
                        run_id,
                        "context",
                        {
                            "provider": provider_name,
                            "source": block.source,
                            "title": block.title,
                            "chars": len(block.content),
                        },
                    )
                except Exception as exc:
                    logger.debug(
                        f"Failed to record context event for provider '{provider_name}': {exc}"
                    )

    return "\n".join(content_parts)
