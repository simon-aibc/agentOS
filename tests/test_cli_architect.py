import os
import shutil
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agent_os.agents.cli_architect import (
    MAX_INVENTORY_FILES,
    _build_architect_prompt,
    _build_sandbox_inventory,
)


def test_build_sandbox_inventory_sorting_and_relative(monkeypatch, tmp_path):
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    monkeypatch.setenv("AGENT_OS_SANDBOX", str(sandbox))

    (sandbox / "b.txt").write_text("b")
    (sandbox / "a.txt").write_text("a")
    d = sandbox / "dir"
    d.mkdir()
    (d / "c.txt").write_text("c")

    inventory = _build_sandbox_inventory()
    assert inventory == ["a.txt", "b.txt", "dir/c.txt"]


def test_build_sandbox_inventory_symlink_safety(monkeypatch, tmp_path):
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    monkeypatch.setenv("AGENT_OS_SANDBOX", str(sandbox))

    outside = tmp_path / "outside.txt"
    outside.write_text("secret")

    inside = sandbox / "inside.txt"
    inside.write_text("ok")

    # Create symlink pointing outside
    symlink_path = sandbox / "escape.txt"
    os.symlink(outside, symlink_path)

    inventory = _build_sandbox_inventory()
    assert inventory == ["inside.txt"]


def test_build_sandbox_inventory_does_not_follow_symlink_cycle(monkeypatch, tmp_path):
    sandbox = tmp_path / "sandbox"
    nested = sandbox / "nested"
    nested.mkdir(parents=True)
    monkeypatch.setenv("AGENT_OS_SANDBOX", str(sandbox))
    (nested / "inside.txt").write_text("ok", encoding="utf-8")
    (nested / "cycle").symlink_to(sandbox, target_is_directory=True)

    assert _build_sandbox_inventory() == ["nested/inside.txt"]


def test_build_sandbox_inventory_skips_generated_and_dependency_trees(
    monkeypatch, tmp_path
):
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    monkeypatch.setenv("AGENT_OS_SANDBOX", str(sandbox))
    (sandbox / "package.json").write_text("{}", encoding="utf-8")
    for directory in (".git", ".graphify", "dist", "node_modules"):
        ignored = sandbox / directory
        ignored.mkdir()
        (ignored / "noise.txt").write_text("noise", encoding="utf-8")

    assert _build_sandbox_inventory() == ["package.json"]


def test_build_sandbox_inventory_bounds(monkeypatch, tmp_path):
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    monkeypatch.setenv("AGENT_OS_SANDBOX", str(sandbox))

    for i in range(MAX_INVENTORY_FILES + 10):
        (sandbox / f"{i:04d}.txt").write_text("")

    inventory = _build_sandbox_inventory()
    assert len(inventory) == MAX_INVENTORY_FILES


def test_build_architect_prompt_basic(monkeypatch, tmp_path):
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    monkeypatch.setenv("AGENT_OS_SANDBOX", str(sandbox))
    (sandbox / "code.py").write_text("print('hi')")

    state = {
        "task": "do work",
        "human_feedback": None,
        "messages": [],
        "plan": None,
        "executor_output": None,
        "hot_context": None,
        "tool_result": None,
        "router_escalated": None,
    }

    prompt = _build_architect_prompt(state)
    assert "Task:\ndo work" in prompt
    assert "- code.py" in prompt
    assert "rejected:" not in prompt
    assert "CodingPlan" in prompt


def test_build_architect_prompt_rejection_feedback(monkeypatch, tmp_path):
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    monkeypatch.setenv("AGENT_OS_SANDBOX", str(sandbox))

    state = {
        "task": "do work",
        "human_feedback": "rejected: bad idea",
        "messages": [],
        "plan": None,
        "executor_output": None,
        "hot_context": None,
        "tool_result": None,
        "router_escalated": None,
    }

    prompt = _build_architect_prompt(state)
    assert "Previous plan was rejected with feedback:\nrejected: bad idea" in prompt


def test_build_cli_architect_invoker_unknown_backend():
    from agent_os.agents.cli_architect import build_cli_architect_invoker

    with pytest.raises(ValueError, match="Unsupported CLI architect backend: unknown"):
        build_cli_architect_invoker("unknown")


def test_build_cli_architect_invoker_claude_success(monkeypatch, tmp_path):
    from unittest.mock import MagicMock

    from agent_os.agents.cli_architect import build_cli_architect_invoker
    from agent_os.schemas import CodingPlan

    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    monkeypatch.setenv("AGENT_OS_SANDBOX", str(sandbox))

    invoker = build_cli_architect_invoker("claude-code")
    state = {"task": "test", "human_feedback": None}

    with patch("agent_os.backends.run_cli_command") as mock_run:
        # Mock run_cli_command returning stream-json style payload
        mock_run.return_value = MagicMock(
            stdout='{"type": "result", "structured_output": {"summary": "s", "files": ["f"], "changes": ["c"], "verify_cmd": "v"}}'
        )
        brief = invoker(state)

        assert isinstance(brief, CodingPlan)
        assert brief.summary == "s"
        assert brief.files == ["f"]
        assert brief.verify_cmd == "v"


def test_build_cli_architect_invoker_codex_success(monkeypatch, tmp_path):
    import json

    from agent_os.agents.cli_architect import build_cli_architect_invoker
    from agent_os.schemas import CodingPlan

    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    monkeypatch.setenv("AGENT_OS_SANDBOX", str(sandbox))

    invoker = build_cli_architect_invoker("codex")
    state = {"task": "test", "human_feedback": None}

    def mock_run_command(binary, args):
        assert binary == "codex"
        # Extract the output path and write the expected json
        idx = args.index("--output-last-message")
        out_path = args[idx + 1]
        with open(out_path, "w") as f:
            json.dump(
                {
                    "summary": "s2",
                    "files": ["f2"],
                    "changes": ["c2"],
                    "verify_cmd": "v2",
                },
                f,
            )
        return MagicMock()

    with patch("agent_os.backends.run_cli_command", side_effect=mock_run_command):
        brief = invoker(state)

        assert isinstance(brief, CodingPlan)
        assert brief.summary == "s2"
        assert brief.files == ["f2"]
        assert brief.verify_cmd == "v2"


def test_codex_adapter_skips_git_repository_check_inside_agent_os_sandbox(monkeypatch, tmp_path):
    import json

    from agent_os.agents.cli_architect import build_cli_architect_invoker
    from agent_os.schemas import CodingPlan

    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    monkeypatch.setenv("AGENT_OS_SANDBOX", str(sandbox))
    invoker = build_cli_architect_invoker("codex")

    def mock_run_command(binary, args):
        assert binary == "codex"
        assert "--skip-git-repo-check" in args
        output_path = args[args.index("--output-last-message") + 1]
        with open(output_path, "w") as output_file:
            json.dump(
                {
                    "summary": "s",
                    "files": ["plan.md"],
                    "changes": ["document plan"],
                    "verify_cmd": "pytest",
                },
                output_file,
            )
        return MagicMock()

    with patch("agent_os.backends.run_cli_command", side_effect=mock_run_command):
        brief = invoker({"task": "test", "human_feedback": None})

    assert isinstance(brief, CodingPlan)
    assert brief.summary == "s"
    assert brief.files == ["plan.md"]
    assert brief.changes == ["document plan"]
    assert brief.verify_cmd == "pytest"


def test_build_cli_architect_invoker_carries_acceptance_criteria(monkeypatch, tmp_path):
    import json

    from agent_os.agents.cli_architect import build_cli_architect_invoker
    from agent_os.schemas import CodingPlan

    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    monkeypatch.setenv("AGENT_OS_SANDBOX", str(sandbox))

    # 1. Claude-code with acceptance_criteria
    invoker_claude = build_cli_architect_invoker("claude-code")
    state = {
        "task": "clarify task",
        "human_feedback": None,
        "strategy_hint": {
            "strategy_id": "clarify-first-v1",
            "version": 1,
            "task_kind": "workflow",
            "selection_reason": "explicit",
            "directive": "surface 3-5 binary criteria in acceptance_criteria",
        },
    }

    with patch("agent_os.backends.run_cli_command") as mock_run:
        mock_run.return_value = MagicMock(
            stdout='{"type": "result", "structured_output": {"summary": "s", "files": ["f"], "changes": ["c"], "verify_cmd": "v", "acceptance_criteria": ["Crit 1", "Crit 2"]}}'
        )
        brief = invoker_claude(state)
        assert isinstance(brief, CodingPlan)
        assert brief.acceptance_criteria == ["Crit 1", "Crit 2"]

    # 2. Codex with acceptance_criteria
    invoker_codex = build_cli_architect_invoker("codex")

    def mock_run_command(binary, args):
        idx = args.index("--output-last-message")
        out_path = args[idx + 1]
        with open(out_path, "w") as f:
            json.dump(
                {
                    "summary": "s_codex",
                    "files": ["f_codex"],
                    "changes": ["c_codex"],
                    "verify_cmd": "v_codex",
                    "acceptance_criteria": ["Crit A", "Crit B", "Crit C"],
                },
                f,
            )
        return MagicMock()

    with patch("agent_os.backends.run_cli_command", side_effect=mock_run_command):
        brief_codex = invoker_codex(state)
        assert isinstance(brief_codex, CodingPlan)
        assert brief_codex.acceptance_criteria == ["Crit A", "Crit B", "Crit C"]


def test_build_cli_architect_invoker_claude_security_contract(monkeypatch, tmp_path):
    import json
    from unittest.mock import MagicMock

    from agent_os.agents.cli_architect import build_cli_architect_invoker

    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    monkeypatch.setenv("AGENT_OS_SANDBOX", str(sandbox))

    invoker = build_cli_architect_invoker("claude-code")
    state = {"task": "test", "human_feedback": None}

    with patch("agent_os.backends.run_cli_command") as mock_run:
        mock_run.return_value = MagicMock(
            stdout='{"type": "result", "structured_output": {"summary": "s", "files": [], "changes": [], "verify_cmd": ""}}'
        )
        invoker(state)

        args = mock_run.call_args.args[1]
        # Claude must receive --permission-mode plan
        assert "--permission-mode" in args
        assert args[args.index("--permission-mode") + 1] == "plan"
        assert "--verbose" in args
        assert "--dangerously-skip-permissions" not in args
        assert "--add-dir" not in args
        # Claude must receive the inline JSON schema
        assert "--json-schema" in args
        schema = json.loads(args[args.index("--json-schema") + 1])
        assert schema["title"] == "CodingPlan"
        assert "additionalProperties" not in schema


def test_build_cli_architect_invoker_codex_security_contract(monkeypatch, tmp_path):
    import json
    import os
    from unittest.mock import MagicMock

    from agent_os.agents.cli_architect import build_cli_architect_invoker

    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    monkeypatch.setenv("AGENT_OS_SANDBOX", str(sandbox))

    invoker = build_cli_architect_invoker("codex")
    state = {"task": "test", "human_feedback": None}

    schema_file = None
    output_file = None

    def mock_run_command(binary, args):
        nonlocal schema_file, output_file
        assert binary == "codex"

        # Codex must receive --sandbox read-only
        assert "--sandbox" in args
        assert args[args.index("--sandbox") + 1] == "read-only"
        assert "--dangerously-bypass-approvals-and-sandbox" not in args
        assert "--add-dir" not in args

        idx_schema = args.index("--output-schema")
        schema_file = args[idx_schema + 1]
        assert os.path.exists(schema_file)
        schema = json.loads(Path(schema_file).read_text(encoding="utf-8"))
        assert schema["additionalProperties"] is False
        assert schema["required"] == list(schema["properties"])
        assert "$defs" not in schema
        assert "proposed_actions" not in schema["properties"]
        assert "metadata" not in schema["properties"]

        idx_output = args.index("--output-last-message")
        output_file = args[idx_output + 1]

        with open(output_file, "w") as f:
            json.dump({"summary": "s", "files": [], "changes": [], "verify_cmd": ""}, f)

        return MagicMock()

    with patch("agent_os.backends.run_cli_command", side_effect=mock_run_command):
        invoker(state)

    # Temporary files should be removed
    assert schema_file is not None and not os.path.exists(schema_file)
    assert output_file is not None and not os.path.exists(output_file)


def test_build_cli_architect_invoker_codex_temp_cleanup_on_exception(
    monkeypatch, tmp_path
):
    import os

    from agent_os.agents.cli_architect import build_cli_architect_invoker

    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    monkeypatch.setenv("AGENT_OS_SANDBOX", str(sandbox))

    invoker = build_cli_architect_invoker("codex")
    state = {"task": "test", "human_feedback": None}

    output_file = None

    def mock_run_command(binary, args):
        nonlocal output_file
        output_file = args[args.index("--output-last-message") + 1]
        raise RuntimeError("CLI failed")

    with patch("agent_os.backends.run_cli_command", side_effect=mock_run_command):
        with pytest.raises(RuntimeError):
            invoker(state)

    assert output_file is not None and not os.path.exists(output_file)


def test_build_cli_architect_invoker_invalid_payload(monkeypatch, tmp_path):
    from unittest.mock import MagicMock

    from agent_os.agents.cli_architect import build_cli_architect_invoker

    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    monkeypatch.setenv("AGENT_OS_SANDBOX", str(sandbox))

    invoker = build_cli_architect_invoker("claude-code")
    state = {"task": "test", "human_feedback": None}

    with patch("agent_os.backends.run_cli_command") as mock_run:
        # Missing required fields
        mock_run.return_value = MagicMock(
            stdout='{"type": "result", "structured_output": {"files": []}}'
        )

        with pytest.raises(ValueError) as exc_info:
            invoker(state)

        assert "Failed to validate claude-code structured output as CodingPlan" in str(
            exc_info.value
        )
        # Should not leak raw payload in validation error message
        assert "structured_output" not in str(exc_info.value).lower()


def test_build_cli_architect_invoker_invalid_json_is_redacted(monkeypatch, tmp_path):
    from agent_os.agents.cli_architect import build_cli_architect_invoker

    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    monkeypatch.setenv("AGENT_OS_SANDBOX", str(sandbox))
    monkeypatch.setenv("TEST_API_KEY", "secret-value")
    invoker = build_cli_architect_invoker("claude-code")

    with patch("agent_os.backends.run_cli_command") as mock_run:
        mock_run.return_value = MagicMock(stdout="invalid api_key=secret-value")
        with pytest.raises(ValueError, match="No valid JSON payload") as exc_info:
            invoker({"task": "test", "human_feedback": None})

    assert "secret-value" not in str(exc_info.value)


@pytest.mark.integration
def test_integration_claude_readonly(monkeypatch, tmp_path):
    from agent_os.agents.cli_architect import build_cli_architect_invoker

    if not shutil.which("claude"):
        pytest.skip("claude binary not found on PATH")

    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    monkeypatch.setenv("AGENT_OS_SANDBOX", str(sandbox))

    sentinel = sandbox / "sentinel.txt"
    original_content = "DO NOT EDIT ME"
    sentinel.write_text(original_content)

    invoker = build_cli_architect_invoker("claude-code")
    state = {
        "task": "Edit sentinel.txt to say 'I WAS HERE' and then return an ArchitectBrief detailing the file you modified.",
        "human_feedback": None,
    }

    # Run the invoker, which should run `claude` in plan mode, preventing edits
    brief = invoker(state)

    # Verify the brief is valid
    assert hasattr(brief, "files")

    # Verify sentinel was not edited
    assert sentinel.read_text() == original_content


@pytest.mark.integration
def test_integration_codex_readonly(monkeypatch, tmp_path):
    from agent_os.agents.cli_architect import build_cli_architect_invoker

    if not shutil.which("codex"):
        pytest.skip("codex binary not found on PATH")

    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    monkeypatch.setenv("AGENT_OS_SANDBOX", str(sandbox))

    sentinel = sandbox / "sentinel.txt"
    original_content = "DO NOT EDIT ME"
    sentinel.write_text(original_content)

    invoker = build_cli_architect_invoker("codex")
    state = {
        "task": "Edit sentinel.txt to say 'I WAS HERE' and then return an ArchitectBrief detailing the file you modified.",
        "human_feedback": None,
    }

    # Run the invoker, which should run `codex` in read-only sandbox mode, preventing edits
    brief = invoker(state)

    # Verify the brief is valid
    assert hasattr(brief, "files")

    # Verify sentinel was not edited
    assert sentinel.read_text() == original_content
