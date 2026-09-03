import os

import pytest

from agent_os.schemas import ArchitectBrief, GrepResult, ReadFileResult
from agent_os.tools import grep, plan_writer, read_file


@pytest.fixture
def test_workspace(tmp_path, monkeypatch):
    """Create a temporary workspace and mock cwd to point to it."""
    monkeypatch.delenv("AGENT_OS_SANDBOX", raising=False)
    monkeypatch.chdir(tmp_path)

    # Create some test files
    (tmp_path / "hello.txt").write_text("Hello World!\nSecond line.", encoding="utf-8")

    subdir = tmp_path / "src"
    subdir.mkdir()
    (subdir / "code.py").write_text("def foo():\n    return 'bar'\n", encoding="utf-8")

    return tmp_path


def test_read_file_success(test_workspace):
    result = read_file.invoke({"path": "hello.txt"})
    assert isinstance(result, ReadFileResult)
    assert result.path == "hello.txt"
    assert "Hello World!" in result.content


def test_read_file_outside_root(test_workspace):
    with pytest.raises(ValueError, match="Path is outside project root"):
        read_file.invoke({"path": "../outside.txt"})


def test_read_file_absolute_outside_root(test_workspace):
    with pytest.raises(ValueError, match="Path is outside project root"):
        # Even if /tmp exists, it's outside the tmp_path root
        read_file.invoke({"path": "/tmp/some_absolute_path.txt"})


def test_grep_success(test_workspace):
    result = grep.invoke({"query": "bar", "path": "."})
    assert isinstance(result, GrepResult)
    assert len(result.matches) == 1
    # Check normalization handles OS separators implicitly or explicitly
    assert result.matches[0].path == os.path.join("src", "code.py")
    assert result.matches[0].line == 2
    assert result.matches[0].text == "    return 'bar'"


def test_grep_outside_root(test_workspace):
    with pytest.raises(ValueError, match="Path is outside project root"):
        grep.invoke({"query": "foo", "path": "../"})


def test_plan_writer(test_workspace):
    before_mtime = (test_workspace / "hello.txt").stat().st_mtime

    result = plan_writer.invoke(
        {"files": ["src/code.py"], "changes": ["Modify foo"], "verify_cmd": "pytest"}
    )

    assert isinstance(result, ArchitectBrief)
    assert result.files == ["src/code.py"]
    assert result.changes == ["Modify foo"]
    assert result.verify_cmd == "pytest"

    # Ensure no side effects on disk
    after_mtime = (test_workspace / "hello.txt").stat().st_mtime
    assert before_mtime == after_mtime


def test_grep_symlink_outside_root(test_workspace, tmp_path_factory):
    # Create a file outside the workspace root
    outside_dir = tmp_path_factory.mktemp("outside")
    outside_file = outside_dir / "secret.txt"
    outside_file.write_text("SUPER_SECRET_PASSWORD_123", encoding="utf-8")

    # Create an in-root symlink pointing to the outside file
    symlink_path = test_workspace / "link_to_secret.txt"
    symlink_path.symlink_to(outside_file)

    # Run grep on the root
    result = grep.invoke({"query": "SUPER_SECRET", "path": "."})

    # The outside file contents should not appear
    assert len(result.matches) == 0
