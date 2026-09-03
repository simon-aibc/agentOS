import io
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from rich.console import Console

from agent_os.cli.app import async_main
from agent_os.workspace import WorkspaceLoadError, compose_workspace, load_workspace


def _write_workspace(
    root: Path,
    *,
    name: str = "demo",
    architect: str = "codex",
    executor: str = "codex",
    department: str | None = "Operations",
    organization: str | None = "ExampleCo",
    skills: list[str] | None = None,
    connectors: list[str] | None = None,
    policy_mode: str = "local",
) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "SYSTEM.md").write_text("# System\nUse the workspace context.\n")
    (root / "CONTEXT.md").write_text("# Context\nSeeded public example context.\n")
    skills = skills or []
    connectors = connectors or ["filesystem", "memory"]
    department_line = f'department = "{department}"\n' if department is not None else ""
    organization_line = (
        f'organization = "{organization}"\n' if organization is not None else ""
    )
    workspace_path = root / "workspace.toml"
    workspace_path.write_text(
        "\n".join(
            [
                f"skills = {skills!r}",
                f"connectors = {connectors!r}",
                "",
                "[workspace]",
                f'name = "{name}"',
                department_line.rstrip(),
                organization_line.rstrip(),
                "",
                "[backends]",
                f'architect = "{architect}"',
                f'executor = "{executor}"',
                "",
                "[memory]",
                'type = "markdown"',
                'path = "."',
                "",
                "[context]",
                'sources = ["SYSTEM.md", "CONTEXT.md"]',
                "max_chars = 4000",
                "max_age_days = 3650",
                "",
                "[policy]",
                f'mode = "{policy_mode}"',
                "",
                "[limits]",
                "recursion_limit = 7",
                "",
            ]
        )
    )
    return workspace_path


def _write_skill_package(root: Path) -> Path:
    package = root / "skills" / "hello_skill"
    package.mkdir(parents=True)
    (package / "manifest.toml").write_text(
        "\n".join(
            [
                "[skill]",
                'name = "hello_skill"',
                'version = "0.1.0"',
                "",
                "[[skill.handlers]]",
                'match = ["hello_skill", "hello"]',
                'entrypoint = "handlers:hello_skill"',
                "",
            ]
        )
    )
    (package / "handlers.py").write_text(
        "def hello_skill(task: str = '', **kwargs):\n"
        "    return {'task': task, 'ok': True}\n"
    )
    return package


def test_load_valid_workspace(tmp_path):
    workspace_path = _write_workspace(tmp_path)

    workspace = load_workspace(workspace_path)

    assert workspace.workspace.name == "demo"
    assert workspace.workspace.department == "Operations"
    assert workspace.workspace.organization == "ExampleCo"
    assert workspace.memory["path"] == str(tmp_path.resolve())
    assert workspace.connectors == ["filesystem", "memory"]


def test_missing_backend_actionable_fail(tmp_path):
    workspace_path = _write_workspace(tmp_path, architect="missing")

    with pytest.raises(WorkspaceLoadError, match="Unknown backend adapter 'missing'"):
        load_workspace(workspace_path)


def test_compose_binds_workspace_runtime(tmp_path, monkeypatch):
    monkeypatch.delenv("LLM_ARCHITECT", raising=False)
    monkeypatch.delenv("LLM_EXECUTOR", raising=False)
    skill_package = _write_skill_package(tmp_path)
    workspace_path = _write_workspace(tmp_path, skills=[str(skill_package)])

    composed = compose_workspace(load_workspace(workspace_path))

    assert composed.backend_binding.architect == "cli/codex"
    assert composed.backend_binding.executor == "cli/codex"
    assert composed.backend_binding.profile_name == "workspace:demo"
    assert composed.skill_registry.get("hello_skill") is not None
    assert composed.connector_registry.resolve("filesystem").name == "filesystem"
    assert composed.connector_registry.resolve("memory").name == "markdown_vault"
    assert composed.memory_connector.name == "markdown_vault"
    assert composed.policy.rules == {"mode": "local"}
    assert "Use the workspace context." in composed.hot_context
    assert composed.limits == {"recursion_limit": 7}
    assert "LLM_ARCHITECT" not in os.environ


def test_public_read_only_workspace_has_no_default_file_or_shell_tools(
    tmp_path,
    monkeypatch,
):
    monkeypatch.delenv("LLM_ARCHITECT", raising=False)
    monkeypatch.delenv("LLM_EXECUTOR", raising=False)
    skill_package = _write_skill_package(tmp_path)
    workspace_path = _write_workspace(
        tmp_path,
        skills=[str(skill_package)],
        policy_mode="public-read-only",
    )

    composed = compose_workspace(load_workspace(workspace_path))

    assert composed.skill_registry.names() == ["hello_skill"]
    assert composed.skill_registry.get("read_file") is None
    assert composed.skill_registry.get("write_file") is None
    assert composed.skill_registry.get("bash") is None


def test_department_metadata_only(tmp_path):
    workspace_path = _write_workspace(
        tmp_path,
        department="People Operations",
        organization="Any Organization String",
    )

    workspace = load_workspace(workspace_path)

    assert workspace.workspace.department == "People Operations"
    assert workspace.workspace.organization == "Any Organization String"


@pytest.mark.anyio
async def test_no_workspace_backward_compat(monkeypatch):
    monkeypatch.setattr(
        "agent_os.profiles.load_profiles",
        lambda: SimpleNamespace(default=None, profiles={}),
    )

    class CompletedGraph:
        def __init__(self):
            self.calls = []

        async def astream_events(self, state, config, version):
            self.calls.append((state, config, version))
            yield {"event": "unknown"}

        async def aget_state(self, config):
            return SimpleNamespace(
                created_at="now", values={"task": "done"}, next=(), tasks=()
            )

    graph = CompletedGraph()
    console = Console(file=io.StringIO(), force_terminal=False, color_system=None)

    code = await async_main(
        ["task", "--thread-id", "no-workspace"],
        graph_factory=lambda: graph,
        console=console,
    )

    state, config, _ = graph.calls[0]
    assert code == 0
    assert state["hot_context"] is None
    assert "workspace" not in config["configurable"]


def test_both_reference_workspaces_load():
    root = Path(__file__).resolve().parents[1]
    workspace_paths = [
        root / "examples" / "coding-maintainer" / "workspace.toml",
        root / "examples" / "knowledge-assistant" / "workspace.toml",
    ]

    for workspace_path in workspace_paths:
        workspace = load_workspace(workspace_path)
        composed = compose_workspace(workspace)

        assert composed.workspace.workspace.name
        assert composed.memory_connector.name == "markdown_vault"
        assert composed.hot_context


class DummyPluginMemory:
    name = "synthetic_memory"
    supported_write_modes = frozenset({"create"})

    def search(self, query: str, limit: int = 3) -> list[dict]:
        return []

    def read_note(self, ref: str) -> dict:
        return {"ref": ref, "content": ""}

    def list_notes(self, filters: dict | None = None) -> list[dict]:
        return []

    def write_note(self, ref: str, content: str, **kwargs: object) -> object:
        return object()

    def describe_write_side_effect(self, ref: str, mode: str) -> str:
        return "write"


class DummyPluginActionConnector:
    name = "github_tool"

    def capabilities(self) -> dict:
        return {"actions": []}

    def describe_side_effect(self, action: str) -> str:
        return "read"

    def invoke(self, action: str, args: dict) -> object:
        return object()


class SyntheticEntryPoint:
    def __init__(self, name: str, target: object = None, error: Exception | None = None):
        self.name = name
        self.target = target
        self.error = error

    def load(self):
        if self.error:
            raise self.error
        return self.target


def test_workspace_external_memory_connector_plugin(tmp_path: Path, monkeypatch):
    eps = [SyntheticEntryPoint("synthetic_mem", target=DummyPluginMemory)]
    monkeypatch.setattr(
        "agent_os.plugins._get_entry_points",
        lambda group: eps if group == "agent_os.memory_connectors" else [],
    )

    workspace_path = _write_workspace(tmp_path)
    # Edit workspace.toml to use synthetic_mem
    content = workspace_path.read_text()
    content = content.replace('type = "markdown"', 'type = "synthetic_mem"')
    workspace_path.write_text(content)

    workspace = load_workspace(workspace_path)
    composed = compose_workspace(workspace)
    assert composed.memory_connector.name == "synthetic_memory"
    assert composed.connector_registry.resolve("memory").name == "synthetic_memory"


def test_workspace_external_action_connector_plugin(tmp_path: Path, monkeypatch):
    eps = [SyntheticEntryPoint("github", target=DummyPluginActionConnector)]
    monkeypatch.setattr(
        "agent_os.plugins._get_entry_points",
        lambda group: eps if group == "agent_os.connectors" else [],
    )

    workspace_path = _write_workspace(
        tmp_path,
        connectors=["filesystem", "memory", "github"],
    )

    workspace = load_workspace(workspace_path)
    composed = compose_workspace(workspace)
    assert composed.connector_registry.resolve("github").name == "github_tool"


def test_workspace_plugin_cannot_override_builtin_memory_name(tmp_path: Path, monkeypatch):
    eps = [SyntheticEntryPoint("markdown", target=DummyPluginMemory)]
    monkeypatch.setattr(
        "agent_os.plugins._get_entry_points",
        lambda group: eps if group == "agent_os.memory_connectors" else [],
    )

    workspace_path = _write_workspace(tmp_path)
    content = workspace_path.read_text()
    content = content.replace('type = "markdown"', 'type = "other_mem"')
    workspace_path.write_text(content)

    with pytest.raises(WorkspaceLoadError, match="Plugin in 'agent_os.memory_connectors' cannot override built-in memory connector 'markdown'"):
        load_workspace(workspace_path)


def test_workspace_plugin_cannot_override_builtin_memory_when_configured_as_markdown(tmp_path: Path, monkeypatch):
    # Regression test: Even when workspace config is standard markdown, an external plugin named 'markdown' must be rejected
    eps = [SyntheticEntryPoint("markdown", target=DummyPluginMemory)]
    monkeypatch.setattr(
        "agent_os.plugins._get_entry_points",
        lambda group: eps if group == "agent_os.memory_connectors" else [],
    )

    workspace_path = _write_workspace(tmp_path)  # Default is type = "markdown"

    with pytest.raises(WorkspaceLoadError, match="Plugin in 'agent_os.memory_connectors' cannot override built-in memory connector 'markdown'"):
        load_workspace(workspace_path)


def test_workspace_normal_builtin_composes_when_no_collision(tmp_path: Path, monkeypatch):
    # Regression test: Normal built-in workspace composes cleanly when no colliding plugin exists
    monkeypatch.setattr(
        "agent_os.plugins._get_entry_points",
        lambda group: [],
    )

    workspace_path = _write_workspace(tmp_path)
    workspace = load_workspace(workspace_path)
    composed = compose_workspace(workspace)
    assert composed.memory_connector.name == "markdown_vault"


def test_workspace_callable_connector_instance(tmp_path: Path, monkeypatch):
    class CallableActionConnector:
        name = "callable_action"

        def __call__(self, *args, **kwargs):
            return "invoked"

        def capabilities(self) -> dict:
            return {"actions": []}

        def describe_side_effect(self, action: str) -> str:
            return "read"

        def invoke(self, action: str, args: dict) -> object:
            return object()

    callable_instance = CallableActionConnector()
    eps = [SyntheticEntryPoint("callable_action", target=callable_instance)]
    monkeypatch.setattr(
        "agent_os.plugins._get_entry_points",
        lambda group: eps if group == "agent_os.connectors" else [],
    )

    workspace_path = _write_workspace(
        tmp_path,
        connectors=["filesystem", "memory", "callable_action"],
    )

    workspace = load_workspace(workspace_path)
    composed = compose_workspace(workspace)
    resolved = composed.connector_registry.resolve("callable_action")
    assert resolved is callable_instance
    assert resolved.name == "callable_action"


def test_workspace_action_connector_valid_zero_arg_factory(tmp_path: Path, monkeypatch):
    def action_factory():
        return DummyPluginActionConnector()

    eps = [SyntheticEntryPoint("factory_action", target=action_factory)]
    monkeypatch.setattr(
        "agent_os.plugins._get_entry_points",
        lambda group: eps if group == "agent_os.connectors" else [],
    )

    workspace_path = _write_workspace(
        tmp_path,
        connectors=["filesystem", "memory", "factory_action"],
    )

    workspace = load_workspace(workspace_path)
    composed = compose_workspace(workspace)
    assert composed.connector_registry.resolve("factory_action").name == "github_tool"


def test_workspace_action_connector_unsupported_factory_args_fails_closed(tmp_path: Path, monkeypatch):
    def bad_action_factory(unsupported_arg: str, another_arg: int):
        return DummyPluginActionConnector()

    eps = [SyntheticEntryPoint("bad_action", target=bad_action_factory)]
    monkeypatch.setattr(
        "agent_os.plugins._get_entry_points",
        lambda group: eps if group == "agent_os.connectors" else [],
    )

    workspace_path = _write_workspace(
        tmp_path,
        connectors=["filesystem", "memory", "bad_action"],
    )

    with pytest.raises(WorkspaceLoadError) as exc_info:
        load_workspace(workspace_path)

    msg = str(exc_info.value)
    assert "Action connector plugin 'bad_action' in group 'agent_os.connectors' requires unsupported arguments" in msg
    assert "unsupported_arg" in msg


def test_workspace_action_connector_requires_connector_contract(tmp_path: Path, monkeypatch):
    class InvalidActionConnector:
        name = "invalid_action"

    eps = [SyntheticEntryPoint("invalid_action", target=InvalidActionConnector)]
    monkeypatch.setattr(
        "agent_os.plugins._get_entry_points",
        lambda group: eps if group == "agent_os.connectors" else [],
    )

    workspace_path = _write_workspace(
        tmp_path,
        connectors=["filesystem", "memory", "invalid_action"],
    )

    with pytest.raises(WorkspaceLoadError, match="does not satisfy the Connector interface"):
        load_workspace(workspace_path)


def test_workspace_memory_connector_missing_required_args_fails_closed(tmp_path: Path, monkeypatch):
    def custom_mem_factory(missing_db_url: str):
        return DummyPluginMemory()

    eps = [SyntheticEntryPoint("needs_db", target=custom_mem_factory)]
    monkeypatch.setattr(
        "agent_os.plugins._get_entry_points",
        lambda group: eps if group == "agent_os.memory_connectors" else [],
    )

    workspace_path = _write_workspace(tmp_path)
    content = workspace_path.read_text()
    content = content.replace('type = "markdown"', 'type = "needs_db"')
    workspace_path.write_text(content)

    with pytest.raises(WorkspaceLoadError) as exc_info:
        load_workspace(workspace_path)

    msg = str(exc_info.value)
    assert "Memory connector plugin 'needs_db' in group 'agent_os.memory_connectors' requires parameter(s)" in msg
    assert "missing_db_url" in msg


def test_workspace_memory_connector_requires_writable_contract(tmp_path: Path, monkeypatch):
    class ReadOnlyMemoryPlugin:
        name = "read_only"

        def search(self, query: str, limit: int = 3) -> list[dict]:
            return []

        def read_note(self, ref: str) -> dict:
            return {"ref": ref, "content": ""}

    eps = [SyntheticEntryPoint("read_only", target=ReadOnlyMemoryPlugin)]
    monkeypatch.setattr(
        "agent_os.plugins._get_entry_points",
        lambda group: eps if group == "agent_os.memory_connectors" else [],
    )

    workspace_path = _write_workspace(tmp_path)
    content = workspace_path.read_text()
    workspace_path.write_text(
        content.replace('type = "markdown"', 'type = "read_only"')
    )

    with pytest.raises(WorkspaceLoadError, match="WritableMemory write methods"):
        load_workspace(workspace_path)


def test_workspace_plugin_cannot_override_builtin_action_name(tmp_path: Path, monkeypatch):
    eps = [SyntheticEntryPoint("filesystem", target=DummyPluginActionConnector)]
    monkeypatch.setattr(
        "agent_os.plugins._get_entry_points",
        lambda group: eps if group == "agent_os.connectors" else [],
    )

    workspace_path = _write_workspace(tmp_path)

    with pytest.raises(WorkspaceLoadError, match="Plugin in 'agent_os.connectors' cannot override built-in connector 'filesystem'"):
        load_workspace(workspace_path)


def test_workspace_unknown_memory_connector_lists_builtins_and_plugins(tmp_path: Path, monkeypatch):
    eps = [SyntheticEntryPoint("vector_store", target=DummyPluginMemory)]
    monkeypatch.setattr(
        "agent_os.plugins._get_entry_points",
        lambda group: eps if group == "agent_os.memory_connectors" else [],
    )

    workspace_path = _write_workspace(tmp_path)
    content = workspace_path.read_text()
    content = content.replace('type = "markdown"', 'type = "nonexistent_memory"')
    workspace_path.write_text(content)

    with pytest.raises(WorkspaceLoadError) as exc_info:
        load_workspace(workspace_path)

    msg = str(exc_info.value)
    assert "Unknown memory connector 'nonexistent_memory'" in msg
    assert "markdown, gbrain" in msg
    assert "vector_store" in msg


def test_workspace_unknown_action_connector_lists_builtins_and_plugins(tmp_path: Path, monkeypatch):
    eps = [SyntheticEntryPoint("slack", target=DummyPluginActionConnector)]
    monkeypatch.setattr(
        "agent_os.plugins._get_entry_points",
        lambda group: eps if group == "agent_os.connectors" else [],
    )

    workspace_path = _write_workspace(
        tmp_path,
        connectors=["filesystem", "memory", "missing_tool"],
    )

    with pytest.raises(WorkspaceLoadError) as exc_info:
        load_workspace(workspace_path)

    msg = str(exc_info.value)
    assert "Unknown workspace connector 'missing_tool'" in msg
    assert "filesystem" in msg
    assert "slack" in msg


def test_workspace_plugin_load_failure_fail_closed(tmp_path: Path, monkeypatch):
    eps = [SyntheticEntryPoint("broken_mem", error=ImportError("Missing C extension"))]
    monkeypatch.setattr(
        "agent_os.plugins._get_entry_points",
        lambda group: eps if group == "agent_os.memory_connectors" else [],
    )

    workspace_path = _write_workspace(tmp_path)
    content = workspace_path.read_text()
    content = content.replace('type = "markdown"', 'type = "broken_mem"')
    workspace_path.write_text(content)

    with pytest.raises(WorkspaceLoadError) as exc_info:
        load_workspace(workspace_path)

    msg = str(exc_info.value)
    assert "Failed to load memory connector plugin 'broken_mem'" in msg
    assert "Missing C extension" in msg


def test_workspace_gbrain_memory_connector_unmodified(tmp_path: Path):
    workspace_path = _write_workspace(tmp_path)
    content = workspace_path.read_text()
    content = content.replace('type = "markdown"', 'type = "gbrain"')
    workspace_path.write_text(content)

    workspace = load_workspace(workspace_path)
    composed = compose_workspace(workspace)
    assert composed.memory_connector.name == "gbrain"
    assert composed.connector_registry.resolve("memory").name == "gbrain"
