from pathlib import Path

import pytest

from agent_os.sandbox import (
    get_read_root,
    get_sandbox_root,
    resolve_sandbox_path,
    resolve_workspace_root,
    sandbox_scope,
)


def test_get_sandbox_root_default(monkeypatch):
    monkeypatch.delenv("AGENT_OS_SANDBOX", raising=False)
    expected = Path.cwd() / "sandbox"
    assert get_sandbox_root() == expected.resolve()


def test_get_sandbox_root_custom_relative(monkeypatch):
    monkeypatch.setenv("AGENT_OS_SANDBOX", "./custom_box")
    expected = Path.cwd() / "custom_box"
    assert get_sandbox_root() == expected.resolve()


def test_get_sandbox_root_custom_absolute(monkeypatch, tmp_path):
    box_path = tmp_path / "abs_box"
    monkeypatch.setenv("AGENT_OS_SANDBOX", str(box_path))
    assert get_sandbox_root() == box_path.resolve()


def test_resolve_sandbox_path_within(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_OS_SANDBOX", str(tmp_path))
    result = resolve_sandbox_path("foo/bar.txt")
    assert result == (tmp_path / "foo" / "bar.txt").resolve()


def test_resolve_sandbox_path_absolute_within(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_OS_SANDBOX", str(tmp_path))
    abs_path = tmp_path / "foo" / "bar.txt"
    result = resolve_sandbox_path(str(abs_path))
    assert result == abs_path.resolve()


def test_resolve_sandbox_path_traversal_escape(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_OS_SANDBOX", str(tmp_path))
    with pytest.raises(ValueError, match="resolves outside the sandbox"):
        resolve_sandbox_path("../escaped.txt")


def test_resolve_sandbox_path_absolute_escape(monkeypatch, tmp_path, tmp_path_factory):
    monkeypatch.setenv("AGENT_OS_SANDBOX", str(tmp_path))
    outside = tmp_path_factory.mktemp("outside") / "escaped.txt"
    with pytest.raises(ValueError, match="resolves outside the sandbox"):
        resolve_sandbox_path(str(outside))


def test_resolve_sandbox_path_symlink_escape(monkeypatch, tmp_path, tmp_path_factory):
    monkeypatch.setenv("AGENT_OS_SANDBOX", str(tmp_path))
    outside = tmp_path_factory.mktemp("outside") / "escaped.txt"
    outside.write_text("secret")

    # Create a symlink inside the sandbox pointing outside
    link = tmp_path / "link.txt"
    link.symlink_to(outside)

    with pytest.raises(ValueError, match="resolves outside the sandbox"):
        resolve_sandbox_path("link.txt")


def test_workspace_scope_overrides_read_and_write_roots(monkeypatch, tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("AGENT_OS_SANDBOX", str(tmp_path))

    with sandbox_scope("project") as resolved:
        assert resolved == project.resolve()
        assert get_sandbox_root() == project.resolve()
        assert get_read_root() == project.resolve()

    assert get_sandbox_root() == tmp_path.resolve()
    assert get_read_root() == tmp_path.resolve()


def test_resolve_workspace_rejects_escape_and_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_OS_SANDBOX", str(tmp_path))

    with pytest.raises(ValueError, match="outside the sandbox"):
        resolve_workspace_root(str(tmp_path.parent))
    with pytest.raises(ValueError, match="not an existing directory"):
        resolve_workspace_root("missing")
