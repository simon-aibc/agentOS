from pathlib import Path

import pytest

from agent_os.api import SkillPackageLoader, SkillRegistry


def _write_package(
    root: Path,
    directory: str,
    *,
    matches: tuple[str, ...] = ("hello",),
    body: str = "def handle(value='ok'):\n    return value\n",
) -> Path:
    package = root / directory
    package.mkdir()
    match_values = ", ".join(repr(item) for item in matches)
    (package / "manifest.toml").write_text(
        "[skill]\n"
        f'name = "{directory}"\n'
        'version = "1.0.0"\n'
        "[[skill.handlers]]\n"
        f"match = [{match_values}]\n"
        'entrypoint = "handlers:handle"\n',
        encoding="utf-8",
    )
    (package / "handlers.py").write_text(body, encoding="utf-8")
    return package


def test_loads_multiple_packages_without_module_name_collisions(tmp_path: Path) -> None:
    first = _write_package(
        tmp_path, "first", matches=("first",), body="def handle():\n    return 1\n"
    )
    second = _write_package(
        tmp_path, "second", matches=("second",), body="def handle():\n    return 2\n"
    )
    registry = SkillRegistry()
    loader = SkillPackageLoader(registry)

    loader.load_package(first)
    loader.load_package(second)

    assert registry.get("first").invoke({}) == 1
    assert registry.get("second").invoke({}) == 2


def test_loads_plain_callable_and_base_tool(tmp_path: Path) -> None:
    _write_package(tmp_path, "plain", matches=("plain", "p"))
    _write_package(
        tmp_path,
        "tool",
        matches=("tool",),
        body=(
            "from langchain_core.tools import tool\n"
            "@tool\n"
            "def handle(value: str = 'ok') -> str:\n"
            '    """Return the input."""\n'
            "    return value\n"
        ),
    )
    registry = SkillRegistry()

    SkillPackageLoader(registry).load_from_directories([str(tmp_path)])

    assert registry.deterministic_match("p").invoke({"value": "plain"}) == "plain"
    assert registry.get("tool").invoke({"value": "base"}) == "base"


@pytest.mark.parametrize(
    ("manifest", "extra_file"),
    [
        (None, None),
        ("not = [valid", None),
        (
            "[skill]\nversion='1'\n[[skill.handlers]]\nmatch=['x']\nentrypoint='handlers:handle'",
            "def handle(): pass",
        ),
        ("[skill]\nname='x'\nversion='1'", None),
        (
            "[skill]\nname='x'\nversion='1'\n[[skill.handlers]]\nmatch=[]\nentrypoint='handlers:handle'",
            "def handle(): pass",
        ),
        (
            "[skill]\nname='x'\nversion='1'\n[[skill.handlers]]\nmatch=['x']\nentrypoint='../bad:handle'",
            None,
        ),
        (
            "[skill]\nname='x'\nversion='1'\n[[skill.handlers]]\nmatch=['x']\nentrypoint='handlers:missing'",
            "VALUE = 1",
        ),
    ],
)
def test_malformed_packages_raise_with_manifest_path(
    tmp_path: Path, manifest: str | None, extra_file: str | None
) -> None:
    package = tmp_path / "bad"
    package.mkdir()
    if manifest is not None:
        (package / "manifest.toml").write_text(manifest, encoding="utf-8")
    if extra_file is not None:
        (package / "handlers.py").write_text(extra_file, encoding="utf-8")

    with pytest.raises(ValueError, match="manifest.toml"):
        SkillPackageLoader(SkillRegistry()).load_package(package)


def test_duplicate_names_and_aliases_fail_through_registry(tmp_path: Path) -> None:
    _write_package(tmp_path, "a", matches=("same", "alias"))
    _write_package(tmp_path, "b", matches=("same",))
    registry = SkillRegistry()

    with pytest.raises(ValueError, match="already registered"):
        SkillPackageLoader(registry).load_from_directories([str(tmp_path)])
