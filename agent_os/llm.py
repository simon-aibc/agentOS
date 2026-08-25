import os
import time
from collections.abc import Callable
from typing import TypeVar

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import SystemMessage
from langchain_litellm import ChatLiteLLM

T = TypeVar("T")
LLM_RETRY_BACKOFF_SECONDS = (2, 4, 8)


def _is_retryable_llm_error(error: Exception) -> bool:
    # CLI executors can have already modified the sandbox before their process
    # timeout is reported. Retrying them automatically risks duplicate or
    # conflicting side effects; an operator must inspect and resume explicitly.
    from agent_os.cli_backends import CliBackendTimeout

    if isinstance(error, CliBackendTimeout):
        return False

    message = str(error).casefold()
    retryable_markers = (
        "429",
        "rate limit",
        "ratelimit",
        "resource_exhausted",
        "503",
        "service unavailable",
        "timeout",
        "timed out",
    )
    return any(marker in message for marker in retryable_markers)


def invoke_with_llm_retry(operation: Callable[[], T]) -> T:
    """Retry transient LLM provider failures with short exponential backoff."""
    last_error: Exception | None = None
    for delay in (0, *LLM_RETRY_BACKOFF_SECONDS):
        if delay:
            time.sleep(delay)
        try:
            return operation()
        except Exception as error:
            if not _is_retryable_llm_error(error):
                raise
            last_error = error

    if last_error is None:
        raise RuntimeError("LLM retry exhausted without an error")
    raise last_error


def prepare_system_prompt(prompt: str, llm: BaseChatModel) -> str | SystemMessage:
    """Add Anthropic's ephemeral cache control to a system prompt."""
    provider = getattr(llm, "custom_llm_provider", None)
    model_values = (
        getattr(llm, "model", None),
        getattr(llm, "model_name", None),
    )
    is_anthropic = (
        isinstance(provider, str) and provider.casefold() == "anthropic"
    ) or any(
        isinstance(model, str) and model.casefold().startswith("anthropic/")
        for model in model_values
    )

    if is_anthropic:
        return SystemMessage(
            content=[
                {
                    "type": "text",
                    "text": prompt,
                    "cache_control": {"type": "ephemeral"},
                }
            ]
        )
    return prompt


def get_architect_llm(model_name: str | None = None) -> BaseChatModel:
    """
    Get the configured architect LLM.
    Resolves model_name explicitly first, then falls back to LLM_ARCHITECT
    environment variable.
    """
    resolved_model = model_name or os.getenv("LLM_ARCHITECT")
    if not resolved_model:
        raise ValueError(
            "No architect model configured. Pass model_name or set LLM_ARCHITECT."
        )

    return ChatLiteLLM(model=resolved_model)


def get_executor_llm(model_name: str | None = None) -> BaseChatModel:
    """
    Get the configured executor LLM.
    Resolves model_name explicitly first, then falls back to LLM_EXECUTOR
    environment variable.
    """
    resolved_model = model_name or os.getenv("LLM_EXECUTOR")
    if not resolved_model:
        raise ValueError(
            "No executor model configured. Pass model_name or set LLM_EXECUTOR."
        )

    return ChatLiteLLM(model=resolved_model)


def get_router_llm(model_name: str | None = None) -> BaseChatModel:
    """
    Get the configured router LLM (cheap model).
    Resolves model_name explicitly first, then falls back to LLM_ROUTER
    environment variable.
    """
    resolved_model = model_name or os.getenv("LLM_ROUTER")
    if not resolved_model:
        raise ValueError(
            "No router model configured. Pass model_name or set LLM_ROUTER."
        )

    return ChatLiteLLM(model=resolved_model)
