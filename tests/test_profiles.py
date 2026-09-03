from pathlib import Path

import pytest

from agent_os.backends import BackendRegistry
from agent_os.profiles import (
    ProfileFile,
    ProfileLoadError,
    load_profiles,
    resolve_profile,
    select_profile_name,
)


class MockAdapter:
    def __init__(self, name, binary_name, roles):
        self.name = name
        self.binary_name = binary_name
        self.supported_roles = roles


def test_select_profile_name():
    assert select_profile_name("a", "b", "c") == ("a", "flag")
    assert select_profile_name(None, "b", "c") == ("b", "env")
    assert select_profile_name(None, None, "c") == ("c", "default")
    assert select_profile_name(None, None, None) == (None, None)


def test_load_missing_file():
    pf = load_profiles(Path("/does/not/exist.toml"))
    assert pf.default is None
    assert not pf.profiles


def test_load_malformed_toml(tmp_path):
    p = tmp_path / "bad.toml"
    p.write_text("[profile.a\nbad")
    with pytest.raises(ProfileLoadError, match="Malformed TOML"):
        load_profiles(p)


def test_load_secret_rejection(tmp_path):
    p = tmp_path / "secret.toml"
    p.write_text('[profile.a]\napi_key = "super_secret"')
    with pytest.raises(ProfileLoadError, match="rejected secret key"):
        try:
            load_profiles(p)
        except Exception as e:
            assert "super_secret" not in str(e)
            raise


def test_load_valid_toml(tmp_path):
    p = tmp_path / "valid.toml"
    p.write_text("""
    default = "cli-base"
    [profile.cli-base]
    router = "ollama/llama3.1"
    architect = "cli/claude-code"
    
    [profile.cli-write]
    extends = "cli-base"
    sandbox = "./worktree-sandbox"
    """)
    pf = load_profiles(p)
    assert pf.default == "cli-base"
    assert "cli-base" in pf.profiles
    assert pf.profiles["cli-base"].name == "cli-base"
    assert pf.profiles["cli-write"].extends == "cli-base"


def test_unknown_keys(tmp_path):
    p = tmp_path / "unknown.toml"
    p.write_text("unknown = 123")
    with pytest.raises(ProfileLoadError, match="Invalid profile configuration"):
        load_profiles(p)

    p2 = tmp_path / "unknown2.toml"
    p2.write_text('[profile.a]\nfoo = "bar"')
    with pytest.raises(ProfileLoadError, match="Invalid profile configuration"):
        load_profiles(p2)


def test_xdg_loading(monkeypatch, tmp_path):
    xdg = tmp_path / "xdg"
    (xdg / "agent-os").mkdir(parents=True)
    p = xdg / "agent-os" / "profiles.toml"
    p.write_text('default = "from-xdg"')
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))

    pf = load_profiles()
    assert pf.default == "from-xdg"


def test_home_config_fallback(monkeypatch, tmp_path):
    home = tmp_path / "home"
    (home / ".config" / "agent-os").mkdir(parents=True)
    p = home / ".config" / "agent-os" / "profiles.toml"
    p.write_text('default = "from-home"')
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)

    # Python's Path.home() might not pick up monkeypatched HOME on all OSes reliably without expanding it,
    # but let's mock Path.home() just in case.
    monkeypatch.setattr(Path, "home", lambda: home)

    pf = load_profiles()
    assert pf.default == "from-home"


def test_resolve_missing_profile():
    pf = ProfileFile(profiles={})
    reg = BackendRegistry()
    with pytest.raises(ProfileLoadError, match="Profile 'a' not found"):
        resolve_profile(pf, "a", reg, Path("/tmp"))


def test_resolve_missing_parent():
    pf = ProfileFile(profiles={"a": {"name": "a", "extends": "b"}})
    reg = BackendRegistry()
    with pytest.raises(ProfileLoadError, match="Parent profile 'b' not found"):
        resolve_profile(pf, "a", reg, Path("/tmp"))


def test_resolve_missing_parent_after_parent_profile():
    pf = ProfileFile(
        profiles={
            "a": {"name": "a", "extends": "b"},
            "b": {"name": "b", "extends": "missing"},
        }
    )
    reg = BackendRegistry()
    with pytest.raises(ProfileLoadError, match="Parent profile 'missing' not found"):
        resolve_profile(pf, "a", reg, Path("/tmp"))


def test_resolve_cycle():
    pf = ProfileFile(profiles={"a": {"name": "a", "extends": "a"}})
    reg = BackendRegistry()
    with pytest.raises(ProfileLoadError, match="Inheritance cycle detected: a -> a"):
        resolve_profile(pf, "a", reg, Path("/tmp"))


def test_resolve_deep_inheritance():
    pf = ProfileFile(
        profiles={
            "a": {"name": "a", "extends": "b"},
            "b": {"name": "b", "extends": "c"},
            "c": {"name": "c"},
        }
    )
    reg = BackendRegistry()
    with pytest.raises(
        ProfileLoadError, match="Deep inheritance is not supported: a -> b -> c"
    ):
        resolve_profile(pf, "a", reg, Path("/tmp"))


def test_resolve_two_profile_cycle():
    pf = ProfileFile(
        profiles={
            "a": {"name": "a", "extends": "b"},
            "b": {"name": "b", "extends": "a"},
        }
    )
    reg = BackendRegistry()
    with pytest.raises(
        ProfileLoadError, match="Inheritance cycle detected: a -> b -> a"
    ):
        resolve_profile(pf, "a", reg, Path("/tmp"))


def test_resolve_unsupported_role():
    pf = ProfileFile(profiles={"a": {"name": "a", "architect": "cli/claude"}})
    reg = BackendRegistry()
    reg.register(
        MockAdapter("claude", "claude", frozenset({"executor"}))
    )  # Only executor

    with pytest.raises(ProfileLoadError, match="does not support role 'architect'"):
        resolve_profile(pf, "a", reg, Path("/tmp"))


def test_resolve_unknown_backend():
    pf = ProfileFile(profiles={"a": {"name": "a", "architect": "cli/missing"}})
    reg = BackendRegistry()
    with pytest.raises(ProfileLoadError, match="Unknown backend adapter 'missing'"):
        resolve_profile(pf, "a", reg, Path("/tmp"))


def test_resolve_router_cli_rejection():
    pf = ProfileFile(profiles={"a": {"name": "a", "router": "cli/claude"}})
    reg = BackendRegistry()
    with pytest.raises(ProfileLoadError, match="Router cannot be a CLI backend"):
        resolve_profile(pf, "a", reg, Path("/tmp"))


def test_resolve_success(tmp_path):
    sandbox_base = tmp_path / "base"
    pf = ProfileFile(
        profiles={
            "parent": {
                "name": "parent",
                "router": "ollama/qwen",
                "architect": "cli/claude",
                "description": "parent desc",
            },
            "child": {
                "name": "child",
                "extends": "parent",
                "sandbox": "~/custom-sandbox",
                "executor": "cli/codex",
                "description": "child desc",
            },
            "minimal": {"name": "minimal"},
        }
    )

    reg = BackendRegistry()
    reg.register(MockAdapter("claude", "claude", frozenset({"architect"})))
    reg.register(MockAdapter("codex", "codex", frozenset({"executor"})))

    resolved_child = resolve_profile(pf, "child", reg, sandbox_base)
    assert resolved_child.name == "child"
    assert resolved_child.router == "ollama/qwen"
    assert resolved_child.architect == "cli/claude"
    assert resolved_child.executor == "cli/codex"
    assert resolved_child.sandbox == str(
        Path("~/custom-sandbox").expanduser().resolve()
    )

    resolved_minimal = resolve_profile(pf, "minimal", reg, sandbox_base)
    assert resolved_minimal.name == "minimal"
    assert resolved_minimal.sandbox == str(sandbox_base)
    assert resolved_minimal.router is None
