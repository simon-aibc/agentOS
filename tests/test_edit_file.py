import pytest

from agent_os.schemas import EditFileResult
from agent_os.tools.edit_file import edit_file


@pytest.fixture
def test_sandbox(tmp_path, monkeypatch):
    """Sets up a temporary sandbox environment."""
    sandbox_dir = tmp_path / "sandbox"
    sandbox_dir.mkdir()
    monkeypatch.setenv("AGENT_OS_SANDBOX", str(sandbox_dir))
    return sandbox_dir


def test_edit_file_successful_create(test_sandbox):
    result = edit_file.invoke({"path": "new_file.txt", "content": "hello world"})
    assert isinstance(result, EditFileResult)

    file_path = test_sandbox / "new_file.txt"
    assert file_path.exists()
    assert file_path.read_text(encoding="utf-8") == "hello world"
    assert result.bytes_written == 11


def test_edit_file_reports_utf8_bytes(test_sandbox):
    content = "Xin chào"
    result = edit_file.invoke({"path": "unicode.txt", "content": content})

    assert (test_sandbox / "unicode.txt").read_text(encoding="utf-8") == content
    assert result.bytes_written == len(content.encode("utf-8"))
    assert result.bytes_written != len(content)


def test_edit_file_successful_overwrite(test_sandbox):
    file_path = test_sandbox / "existing.txt"
    file_path.write_text("old content")

    result = edit_file.invoke({"path": "existing.txt", "content": "new content"})

    assert file_path.read_text(encoding="utf-8") == "new content"
    assert result.bytes_written == 11


def test_edit_file_nested_path_creation(test_sandbox):
    edit_file.invoke({"path": "deep/nested/dir/file.txt", "content": "nested"})

    file_path = test_sandbox / "deep/nested/dir/file.txt"
    assert file_path.exists()
    assert file_path.read_text(encoding="utf-8") == "nested"


def test_edit_file_traversal_rejection(test_sandbox):
    with pytest.raises(ValueError, match="resolves outside the sandbox"):
        edit_file.invoke({"path": "../escape.txt", "content": "bad"})


def test_edit_file_absolute_outside_root_rejection(test_sandbox, tmp_path):
    outside_file = tmp_path / "outside.txt"
    with pytest.raises(ValueError, match="resolves outside the sandbox"):
        edit_file.invoke({"path": str(outside_file), "content": "bad"})
    assert not outside_file.exists()


def test_edit_file_symlink_escape_rejection(test_sandbox, tmp_path):
    outside_file = tmp_path / "outside.txt"
    outside_file.write_text("safe")

    link = test_sandbox / "link.txt"
    link.symlink_to(outside_file)

    with pytest.raises(ValueError, match="resolves outside the sandbox"):
        edit_file.invoke({"path": "link.txt", "content": "bad"})

    assert outside_file.read_text() == "safe"


def test_edit_file_directory_rejection(test_sandbox):
    dir_path = test_sandbox / "my_dir"
    dir_path.mkdir()

    with pytest.raises(ValueError, match="resolves to a directory"):
        edit_file.invoke({"path": "my_dir", "content": "bad"})
