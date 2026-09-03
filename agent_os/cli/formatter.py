import json
from typing import Any

from rich.console import Console
from rich.text import Text

SENSITIVE_KEYS = {
    "api_key",
    "apikey",
    "authorization",
    "credential",
    "credentials",
    "password",
    "secret",
    "token",
}


def _redact_sensitive(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: "***"
            if str(key).lower() in SENSITIVE_KEYS
            else _redact_sensitive(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact_sensitive(item) for item in value]
    return value


class EventFormatter:
    """Render a compact, stable view of LangGraph progress."""

    def __init__(self, console: Console | None = None) -> None:
        self.console = console if console is not None else Console()
        self.warning_console = console if console is not None else Console(stderr=True)
        self._stream_open = False

    def finish_stream(self) -> None:
        """Finish an open token stream before printing structural output."""
        if self._stream_open:
            self.console.print()
            self._stream_open = False

    def _format_compact(self, value: Any) -> str:
        if value is None:
            return ""
        redacted = _redact_sensitive(value)
        if isinstance(redacted, (dict, list, tuple, bool, int, float)):
            return json.dumps(
                redacted,
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            )
        return str(redacted).strip()

    @staticmethod
    def _truncate(value: str, limit: int) -> str:
        if len(value) <= limit:
            return value
        return f"{value[: limit - 3]}..."

    def _print_labeled(self, label: str, message: str, style: str) -> None:
        self.finish_stream()
        text = Text(f"[{label}] ", style=style)
        text.append(message)
        self.console.print(text)

    def print_thread_id(self, thread_id: str) -> None:
        self._print_labeled("THREAD", thread_id, "bold blue")

    def print_supervisor_route(self, destination: str) -> None:
        self._print_labeled("SUPERVISOR", f"→ {destination}", "bold magenta")

    def print_tool_start(self, name: str, arguments: Any) -> None:
        rendered = self._truncate(self._format_compact(arguments), 200)
        self._print_labeled("TOOL", f"{name}({rendered})", "bold cyan")

    def print_tool_result(self, output: Any) -> None:
        rendered = self._format_compact(output)
        lines = rendered.splitlines()
        if len(lines) > 5:
            rendered = "\n".join(lines[:5]) + "\n..."
        rendered = self._truncate(rendered, 500)
        self._print_labeled("RESULT", rendered, "bold green")

    def print_info(self, message: str) -> None:
        self._print_labeled("INFO", message, "bold yellow")

    def print_error(self, message: str) -> None:
        self._print_labeled("ERROR", message, "bold red")

    def print_warning(self, message: str) -> None:
        self.finish_stream()
        text = Text("[WARNING] ", style="bold yellow")
        text.append(message)
        self.warning_console.print(text)

    def print_human_prompt(self, message: str) -> None:
        self._print_labeled("HUMAN", message, "bold green")

    def print_token(self, token: str) -> None:
        if not token:
            return
        self.console.print(Text(token, style="dim"), end="", soft_wrap=True)
        self._stream_open = True
