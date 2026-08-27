import io
import os
import subprocess
import sys
from pathlib import Path

from langchain_core.messages import AIMessageChunk
from langgraph.types import Command
from rich.console import Console

from agent_os.cli.formatter import EventFormatter
from agent_os.cli.parser import format_event


def make_formatter() -> tuple[EventFormatter, io.StringIO]:
    output = io.StringIO()
    console = Console(
        file=output,
        force_terminal=False,
        color_system=None,
        width=120,
    )
    return EventFormatter(console), output


def test_formatter_structural_output_and_stream_boundary():
    formatter, output = make_formatter()

    formatter.print_thread_id("thread-123")
    formatter.print_token("hello")
    formatter.print_token(" world")
    formatter.print_supervisor_route("architect")
    formatter.print_tool_start("read_file", {"path": "src/main.py"})
    formatter.print_tool_result({"success": True})
    formatter.print_human_prompt("Approve plan?")

    assert output.getvalue() == (
        "[THREAD] thread-123\n"
        "hello world\n"
        "[SUPERVISOR] → architect\n"
        '[TOOL] read_file({"path": "src/main.py"})\n'
        '[RESULT] {"success": true}\n'
        "[HUMAN] Approve plan?\n"
    )


def test_formatter_preserves_falsey_values_and_redacts_credentials():
    formatter, output = make_formatter()

    formatter.print_tool_start(
        "remote",
        {"api_key": "secret-value", "count": 0, "enabled": False},
    )
    formatter.print_tool_result(False)

    rendered = output.getvalue()
    assert "secret-value" not in rendered
    assert '"api_key": "***"' in rendered
    assert '"count": 0' in rendered
    assert '"enabled": false' in rendered
    assert "[RESULT] false" in rendered


def test_formatter_truncates_large_multiline_results():
    formatter, output = make_formatter()
    formatter.print_tool_result("\n".join(f"line {number}" for number in range(10)))

    rendered = output.getvalue()
    assert "line 0" in rendered
    assert "line 4" in rendered
    assert "line 5" not in rendered
    assert rendered.rstrip().endswith("...")


def test_parser_formats_tokens_tools_and_supervisor_commands():
    formatter, output = make_formatter()
    events = [
        {
            "event": "on_chat_model_stream",
            "data": {"chunk": AIMessageChunk(content="token")},
        },
        {
            "event": "on_chat_model_stream",
            "data": {
                "chunk": AIMessageChunk(content=[{"type": "text", "text": " block"}])
            },
        },
        {
            "event": "on_tool_start",
            "name": "read_file",
            "data": {"input": {"path": "sample.txt"}},
        },
        {
            "event": "on_tool_end",
            "name": "read_file",
            "data": {"output": "contents"},
        },
        {
            "event": "on_chain_end",
            "name": "supervisor_node",
            "metadata": {"langgraph_node": "supervisor"},
            "data": {"output": Command(goto="executor")},
        },
    ]

    for event in events:
        format_event(event, formatter)

    assert output.getvalue() == (
        "token block\n"
        '[TOOL] read_file({"path": "sample.txt"})\n'
        "[RESULT] contents\n"
        "[SUPERVISOR] → executor\n"
    )


def test_parser_ignores_malformed_events_and_limits_verbose_output():
    formatter, output = make_formatter()

    for malformed in (None, "event", {}, {"event": "on_tool_start"}):
        format_event(malformed, formatter)
    format_event(
        {"event": "on_chain_end", "name": "planner", "data": {}},
        formatter,
        verbose=True,
    )

    assert output.getvalue() == "[INFO] Node 'planner' completed.\n"


def test_python_module_cli_runs_tier1_offline(tmp_path):
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    (sandbox / "sample.txt").write_text("offline contents", encoding="utf-8")

    environment = os.environ.copy()
    for key in list(environment):
        if key.startswith("LLM_") or key.endswith("_API_KEY"):
            environment.pop(key)
    environment.update(
        {
            "AGENT_OS_SANDBOX": str(sandbox),
            "AGENT_OS_CHECKPOINTS_DB": str(tmp_path / "checkpoints.db"),
            "LANGGRAPH_STRICT_MSGPACK": "true",
            "LITELLM_LOCAL_MODEL_COST_MAP": "True",
        }
    )

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "agent_os",
            "read_file sample.txt",
            "--thread-id",
            "offline-cli-test",
            "--sandbox",
            str(sandbox),
        ],
        cwd=Path(__file__).parents[1],
        env=environment,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )

    combined_output = result.stdout + result.stderr
    assert result.returncode == 0, combined_output
    assert "[THREAD] offline-cli-test" in result.stdout
    assert "[SUPERVISOR] → tool_dispatcher" in result.stdout
    assert "[TOOL] read_file" in result.stdout
    assert "[RESULT]" in result.stdout
    assert "offline contents" in result.stdout
    assert "Failed to fetch remote model cost map" not in combined_output
    assert "Deserializing unregistered type" not in combined_output


def test_installed_console_script_help():
    console_script = Path(sys.executable).with_name("agent-os")
    result = subprocess.run(
        [str(console_script), "--help"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0
    assert "Run the Agent OS LangGraph workflow" in result.stdout


def test_installed_console_script_help_run():
    console_script = Path(sys.executable).with_name("agent-os")
    result = subprocess.run(
        [str(console_script), "run", "--help"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0
    assert "--thread-id" in result.stdout

    assert "--resume" in result.stdout


def test_doctor_mode_dispatch(monkeypatch, capsys):
    # Mock at the source module
    import agent_os.cli.doctor
    from agent_os.cli.app import main

    calls: list[tuple[bool, str | None]] = []

    def fake_run_doctor(json_output, workspace_path=None):
        calls.append((json_output, workspace_path))
        return (0, "DOCTOR OK")

    monkeypatch.setattr(agent_os.cli.doctor, "run_doctor", fake_run_doctor)

    result = main(["doctor"])
    assert result == 0
    captured = capsys.readouterr()
    assert "DOCTOR OK" in captured.out

    result2 = main(["doctor", "--json"])
    assert result2 == 0
    captured2 = capsys.readouterr()
    assert "DOCTOR OK" in captured2.out
    assert calls[-1] == (True, None)

    result3 = main(["doctor", "--workspace", "ws.toml"])
    assert result3 == 0
    capsys.readouterr()
    assert calls[-1] == (False, "ws.toml")
