from pathlib import Path

import pytest

from agent_os.skill_packages import SkillPackageLoader
from agent_os.skills import RegisteredSkill, SkillRegistry
from agent_os.workspace import compose_workspace, load_workspace


class SyntheticEntryPoint:
    def __init__(self, name: str, target: object = None, error: Exception | None = None):
        self.name = name
        self.target = target
        self.error = error

    def load(self):
        if self.error:
            raise self.error
        return self.target


def test_skill_package_plugin_loads_registered_skill(monkeypatch):
    skill = RegisteredSkill(
        name="plugin_skill",
        aliases=["pskill"],
        handler=lambda task="", **kwargs: {"ok": True, "task": task},
    )

    eps = [SyntheticEntryPoint("plugin_skill", target=skill)]
    monkeypatch.setattr(
        "agent_os.plugins._get_entry_points",
        lambda group: eps if group == "agent_os.skill_packages" else [],
    )

    registry = SkillRegistry()
    loader = SkillPackageLoader(registry)
    loader.load_from_entry_points()

    assert registry.get("plugin_skill") is not None
    assert registry.deterministic_match("pskill") is not None
    assert registry.get("plugin_skill").invoke({"task": "test"}) == {"ok": True, "task": "test"}


def test_skill_package_plugin_loads_from_directory(tmp_path: Path, monkeypatch):
    pkg_dir = tmp_path / "custom_skill_pkg"
    pkg_dir.mkdir()
    (pkg_dir / "manifest.toml").write_text(
        """
[skill]
name = "custom_pkg"
version = "0.1.0"

[[skill.handlers]]
match = ["custom_pkg_tool"]
entrypoint = "handlers:run"
""",
        encoding="utf-8",
    )
    (pkg_dir / "handlers.py").write_text(
        "def run(**kwargs):\n    return 'executed'\n",
        encoding="utf-8",
    )

    eps = [SyntheticEntryPoint("custom_pkg", target=pkg_dir)]
    monkeypatch.setattr(
        "agent_os.plugins._get_entry_points",
        lambda group: eps if group == "agent_os.skill_packages" else [],
    )

    registry = SkillRegistry()
    loader = SkillPackageLoader(registry)
    loader.load_from_entry_points()

    assert registry.get("custom_pkg_tool") is not None
    assert registry.get("custom_pkg_tool").invoke({}) == "executed"


def test_skill_package_plugin_collision_fails_closed(monkeypatch):
    skill1 = RegisteredSkill(
        name="duplicate_skill",
        aliases=[],
        handler=lambda **kwargs: None,
    )
    skill2 = RegisteredSkill(
        name="duplicate_skill",
        aliases=[],
        handler=lambda **kwargs: None,
    )

    eps = [
        SyntheticEntryPoint("skill_a", target=skill1),
        SyntheticEntryPoint("skill_b", target=skill2),
    ]
    monkeypatch.setattr(
        "agent_os.plugins._get_entry_points",
        lambda group: eps if group == "agent_os.skill_packages" else [],
    )

    registry = SkillRegistry()
    loader = SkillPackageLoader(registry)

    with pytest.raises(ValueError, match="already registered: duplicate_skill"):
        loader.load_from_entry_points()


def test_workspace_loads_skill_package_plugin(tmp_path: Path, monkeypatch):
    skill = RegisteredSkill(
        name="workspace_plugin_tool",
        aliases=[],
        handler=lambda **kwargs: "hello from plugin",
    )

    eps = [SyntheticEntryPoint("ws_plugin", target=skill)]
    monkeypatch.setattr(
        "agent_os.plugins._get_entry_points",
        lambda group: eps if group == "agent_os.skill_packages" else [],
    )

    workspace_path = tmp_path / "workspace.toml"
    workspace_path.write_text(
        """
[workspace]
name = "skill-plugin-ws"

[backends]
architect = "codex"
executor = "codex"

[memory]
type = "markdown"
path = "."

skills = []
""",
        encoding="utf-8",
    )

    ws = load_workspace(workspace_path)
    composed = compose_workspace(ws)

    assert composed.skill_registry.get("workspace_plugin_tool") is not None
    assert composed.skill_registry.get("workspace_plugin_tool").invoke({}) == "hello from plugin"
