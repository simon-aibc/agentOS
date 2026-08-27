from unittest.mock import MagicMock, patch

import pytest
from langchain_core.messages import SystemMessage

from agent_os.agents.architect import (
    SYSTEM_PROMPT as ARCHITECT_SYSTEM_PROMPT,
)
from agent_os.agents.architect import build_architect_agent
from agent_os.agents.executor import (
    SYSTEM_PROMPT as EXECUTOR_SYSTEM_PROMPT,
)
from agent_os.agents.executor import build_executor_agent
from agent_os.llm import (
    get_architect_llm,
    get_executor_llm,
    get_router_llm,
    invoke_with_llm_retry,
    prepare_system_prompt,
)


def test_get_architect_llm_missing_config(monkeypatch):
    """Missing configuration raises ValueError."""
    monkeypatch.delenv("LLM_ARCHITECT", raising=False)
    with pytest.raises(ValueError, match="No architect model configured"):
        get_architect_llm()


@patch("agent_os.llm.ChatLiteLLM")
def test_get_architect_llm_explicit_model(mock_chat, monkeypatch):
    """Explicit model_name takes precedence over environment variable."""
    monkeypatch.setenv("LLM_ARCHITECT", "anthropic/claude-3-haiku-20240307")

    get_architect_llm(model_name="openai/gpt-4o")

    mock_chat.assert_called_once_with(model="openai/gpt-4o")


@patch("agent_os.llm.ChatLiteLLM")
def test_get_executor_llm_missing_config(mock_chat_lite_llm, monkeypatch):
    """Missing configuration raises ValueError."""
    monkeypatch.delenv("LLM_EXECUTOR", raising=False)
    with pytest.raises(ValueError, match="No executor model configured"):
        get_executor_llm()


@patch("agent_os.llm.ChatLiteLLM")
def test_get_executor_llm_explicit_model(mock_chat_lite_llm, monkeypatch):
    """Explicit model_name takes precedence over environment."""
    monkeypatch.setenv("LLM_EXECUTOR", "env-model")
    llm = get_executor_llm(model_name="explicit-model")
    assert llm == mock_chat_lite_llm.return_value
    mock_chat_lite_llm.assert_called_once_with(model="explicit-model")


@patch("agent_os.llm.ChatLiteLLM")
def test_get_executor_llm_env_model(mock_chat_lite_llm, monkeypatch):
    """Environment value is used when no argument is supplied."""
    monkeypatch.setenv("LLM_EXECUTOR", "env-model")
    llm = get_executor_llm()
    assert llm == mock_chat_lite_llm.return_value
    mock_chat_lite_llm.assert_called_once_with(model="env-model")


@patch("agent_os.llm.ChatLiteLLM")
def test_get_architect_llm_env_fallback(mock_chat, monkeypatch):
    """Environment value is used when no argument is supplied."""
    monkeypatch.setenv("LLM_ARCHITECT", "anthropic/claude-3-5-sonnet-20240620")

    get_architect_llm()

    mock_chat.assert_called_once_with(model="anthropic/claude-3-5-sonnet-20240620")


def test_get_router_llm_missing_config(monkeypatch):
    monkeypatch.delenv("LLM_ROUTER", raising=False)
    with pytest.raises(ValueError, match="No router model configured"):
        get_router_llm()


@patch("agent_os.llm.ChatLiteLLM")
def test_get_router_llm_explicit_model(mock_chat, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER", "env-model")
    get_router_llm(model_name="explicit-model")
    mock_chat.assert_called_once_with(model="explicit-model")


@patch("agent_os.llm.ChatLiteLLM")
def test_get_router_llm_env_fallback(mock_chat, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER", "ollama/qwen2.5:14b")
    get_router_llm()
    mock_chat.assert_called_once_with(model="ollama/qwen2.5:14b")


@pytest.mark.parametrize(
    ("patch_target", "factory", "expected_text"),
    [
        (
            "agent_os.agents.architect.create_agent",
            build_architect_agent,
            ARCHITECT_SYSTEM_PROMPT,
        ),
        (
            "agent_os.agents.executor.create_agent",
            build_executor_agent,
            EXECUTOR_SYSTEM_PROMPT,
        ),
    ],
)
def test_anthropic_agent_system_prompts_enable_ephemeral_caching(
    patch_target,
    factory,
    expected_text,
):
    mock_llm = MagicMock()
    mock_llm.model = "anthropic/claude-sonnet-4-5"

    with patch(patch_target) as mock_create:
        factory(llm=mock_llm)

    prompt = mock_create.call_args.kwargs["system_prompt"]
    assert isinstance(prompt, SystemMessage)
    assert prompt.content == [
        {
            "type": "text",
            "text": expected_text,
            "cache_control": {"type": "ephemeral"},
        }
    ]


def test_non_anthropic_system_prompt_remains_plain_text():
    mock_llm = MagicMock()
    mock_llm.model = "openai/gpt-4o"

    prompt = prepare_system_prompt("system instructions", mock_llm)

    assert prompt == "system instructions"


@patch("agent_os.llm.time.sleep")
def test_invoke_with_llm_retry_retries_transient_rate_limits(mock_sleep):
    operation = MagicMock(
        side_effect=[
            RuntimeError("429 RESOURCE_EXHAUSTED"),
            RuntimeError("503 service unavailable"),
            {"structured_response": "ok"},
        ]
    )

    result = invoke_with_llm_retry(operation)

    assert result == {"structured_response": "ok"}
    assert operation.call_count == 3
    assert [call.args[0] for call in mock_sleep.call_args_list] == [2, 4]


@patch("agent_os.llm.time.sleep")
def test_invoke_with_llm_retry_does_not_retry_non_transient_errors(mock_sleep):
    operation = MagicMock(side_effect=ValueError("invalid structured response"))

    with pytest.raises(ValueError, match="invalid structured response"):
        invoke_with_llm_retry(operation)

    operation.assert_called_once()
    mock_sleep.assert_not_called()


@patch("agent_os.llm.time.sleep")
def test_cli_backend_timeout_is_not_retried(mock_sleep):
    from agent_os.cli_backends import CliBackendTimeout

    attempts = 0

    def operation():
        nonlocal attempts
        attempts += 1
        raise CliBackendTimeout("executor timed out")

    with pytest.raises(CliBackendTimeout):
        invoke_with_llm_retry(operation)

    assert attempts == 1
    mock_sleep.assert_not_called()
