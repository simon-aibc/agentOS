import json
import os
from unittest.mock import MagicMock, patch

import pytest

from agent_os.connectors import (
    ConnectorRegistry,
    FilesystemConnector,
    GbrainConnector,
    MarkdownVaultConnector,
)


def test_connector_registry():
    registry = ConnectorRegistry()
    fs = FilesystemConnector()
    registry.register(fs.name, fs)

    assert registry.list_connectors() == ["filesystem"]
    assert registry.resolve("filesystem") == fs

    with pytest.raises(ValueError, match="already registered"):
        registry.register("filesystem", fs)

    with pytest.raises(ValueError, match="not found in registry"):
        registry.resolve("non_existent")


def test_filesystem_connector(monkeypatch, tmp_path):
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    monkeypatch.setenv("AGENT_OS_SANDBOX", str(sandbox))

    (sandbox / "test.txt").write_text("hello world")

    fs = FilesystemConnector()

    # test read
    res = fs.invoke("read", {"path": "test.txt"})
    assert res.status == "completed"
    assert res.outputs["content"] == "hello world"

    # test list
    res = fs.invoke("list", {"path": "."})
    assert res.status == "completed"
    assert "test.txt" in res.outputs["items"]

    # test unsupported
    res = fs.invoke("write", {"path": "test.txt"})
    assert res.status == "failed"
    assert "Unsupported action" in res.errors[0]

    # test escape
    res = fs.invoke("read", {"path": "../outside.txt"})
    assert res.status == "failed"
    assert "resolves outside the sandbox" in res.errors[0]


def test_markdown_vault_connector(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()

    (vault / "note1.md").write_text(
        "---\ntitle: Note 1\ntags: test\n---\nHello [[note2]]"
    )
    (vault / "nested").mkdir()
    (vault / "nested" / "note2.md").write_text("# Nested Note 2\nJust a nested note")

    md = MarkdownVaultConnector(str(vault))

    # list
    notes = md.list_notes()
    refs = [n["ref"] for n in notes]
    assert "note1.md" in refs
    assert "nested/note2.md" in refs

    # verify title
    note1_item = next(n for n in notes if n["ref"] == "note1.md")
    assert note1_item["title"] == "Note 1"
    note2_item = next(n for n in notes if n["ref"] == "nested/note2.md")
    assert note2_item["title"] == "Nested Note 2"

    # search
    res = md.search("nested")
    assert len(res) == 1
    assert res[0]["ref"] == "nested/note2.md"
    assert res[0]["title"] == "Nested Note 2"
    assert "score" in res[0]

    # read
    note = md.read_note("note1")
    assert note["ref"] == "note1"
    assert note["frontmatter"]["tags"] == "test"
    assert "note2" in note["links"]


def test_gbrain_connector_not_configured(monkeypatch):
    monkeypatch.delenv("GBRAIN_TOKEN", raising=False)
    gbrain = GbrainConnector()
    with pytest.raises(RuntimeError, match="gbrain not configured"):
        gbrain.search("test")


def test_gbrain_connector_configured(monkeypatch):
    monkeypatch.setenv("GBRAIN_TOKEN", "test-token")
    monkeypatch.setenv("GBRAIN_URL", "http://test/mcp")
    gbrain = GbrainConnector()

    with patch("urllib.request.urlopen") as mock_urlopen:
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps(
            {
                "result": {
                    "content": [
                        {
                            "text": json.dumps(
                                {
                                    "hits": [
                                        {
                                            "slug": "remote.md",
                                            "title": "Remote",
                                            "chunk_text": "text",
                                            "score": 0.9,
                                        }
                                    ]
                                }
                            )
                        }
                    ]
                }
            }
        ).encode("utf-8")
        mock_response.__enter__.return_value = mock_response
        mock_urlopen.return_value = mock_response

        res = gbrain.search("test")
        assert len(res) == 1
        assert res[0]["ref"] == "remote.md"
        assert res[0]["title"] == "Remote"
        assert res[0]["snippet"] == "text"
        assert res[0]["score"] == 0.9


def test_gbrain_read_note_uses_get_page(monkeypatch):
    monkeypatch.setenv("GBRAIN_TOKEN", "test-token")
    gbrain = GbrainConnector()

    with patch("urllib.request.urlopen") as mock_urlopen:
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps(
            {
                "result": {
                    "content": [
                        {
                            "text": json.dumps(
                                {"compiled_truth": "hello", "title": "test"}
                            )
                        }
                    ]
                }
            }
        ).encode("utf-8")
        mock_response.__enter__.return_value = mock_response
        mock_urlopen.return_value = mock_response

        res = gbrain.read_note("slug-123")
        assert res["ref"] == "slug-123"
        assert res["content"] == "hello"

        # assert payload sent
        req = mock_urlopen.call_args[0][0]
        payload = json.loads(req.data.decode("utf-8"))
        assert payload["method"] == "tools/call"
        assert payload["params"]["name"] == "get_page"
        assert payload["params"]["arguments"]["slug"] == "slug-123"


def test_gbrain_list_notes_uses_list_pages(monkeypatch):
    monkeypatch.setenv("GBRAIN_TOKEN", "test-token")
    gbrain = GbrainConnector()

    with patch("urllib.request.urlopen") as mock_urlopen:
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps(
            {
                "result": {
                    "content": [
                        {
                            "text": json.dumps(
                                {"pages": [{"slug": "p1", "title": "Page 1"}]}
                            )
                        }
                    ]
                }
            }
        ).encode("utf-8")
        mock_response.__enter__.return_value = mock_response
        mock_urlopen.return_value = mock_response

        res = gbrain.list_notes()
        assert len(res) == 1
        assert res[0]["ref"] == "p1"
        assert res[0]["title"] == "Page 1"

        # assert payload sent
        req = mock_urlopen.call_args[0][0]
        payload = json.loads(req.data.decode("utf-8"))
        assert payload["method"] == "tools/call"
        assert payload["params"]["name"] == "list_pages"


@pytest.fixture
def memory_connectors(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "test.md").write_text("---\ntitle: test\n---\nbody [[link]]")

    md_conn = MarkdownVaultConnector(str(vault))

    monkeypatch.setenv("GBRAIN_TOKEN", "test-token")
    gb_conn = GbrainConnector()

    return [md_conn, gb_conn]


@pytest.mark.parametrize("idx", [0, 1])
def test_memory_connector_contract(memory_connectors, idx):
    conn = memory_connectors[idx]

    if isinstance(conn, GbrainConnector):
        # mock it
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_response = MagicMock()

            def side_effect(req, **kwargs):
                payload = json.loads(req.data.decode("utf-8"))
                name = payload["params"]["name"]
                if name == "query":
                    data = {
                        "hits": [
                            {
                                "slug": "test.md",
                                "title": "test",
                                "chunk_text": "body [[link]]",
                            }
                        ]
                    }
                elif name == "get_page":
                    data = {"compiled_truth": "---\ntitle: test\n---\nbody [[link]]"}
                else:
                    data = {"pages": [{"slug": "test.md", "title": "test"}]}
                mock_response.read.return_value = json.dumps(
                    {"result": {"content": [{"text": json.dumps(data)}]}}
                ).encode("utf-8")
                return mock_response

            mock_urlopen.side_effect = side_effect
            mock_response.__enter__.return_value = mock_response

            _run_contract(conn)
    else:
        _run_contract(conn)


def _run_contract(conn):
    res_list = conn.list_notes()
    assert len(res_list) > 0
    assert "ref" in res_list[0]

    res_search = conn.search("body")
    assert len(res_search) > 0
    assert "ref" in res_search[0]
    assert "title" in res_search[0]
    assert "snippet" in res_search[0]
    assert "score" in res_search[0]

    res_read = conn.read_note(res_search[0]["ref"])
    assert "ref" in res_read
    assert "content" in res_read
    assert "frontmatter" in res_read
    assert "links" in res_read
    assert res_read["links"] == ["link"]


@pytest.mark.integration
def test_gbrain_real_smoke():
    if not os.getenv("GBRAIN_TOKEN"):
        pytest.skip("GBRAIN_TOKEN not provided")

    gbrain = GbrainConnector()
    res = gbrain.search("agent-os")
    if not res:
        pytest.skip("No agent-os results in real vault")

    hit = res[0]
    assert "ref" in hit

    note = gbrain.read_note(hit["ref"])
    assert note["content"]
    assert isinstance(note["links"], list)


def test_markdown_write_create(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    md = MarkdownVaultConnector(str(vault))

    res = md.write_note("new_note", "Hello World", mode="create")
    assert res.committed is True
    assert res.mode == "create"
    assert res.ref == "new_note.md"

    content = (vault / "new_note.md").read_text(encoding="utf-8")
    assert content == "Hello World"


def test_markdown_write_create_conflict(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "existing.md").write_text("old", encoding="utf-8")
    md = MarkdownVaultConnector(str(vault))

    with pytest.raises(FileExistsError):
        md.write_note("existing.md", "new", mode="create")


def test_markdown_write_append(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    md = MarkdownVaultConnector(str(vault))

    md.write_note("log.md", "Line 1", mode="append")
    md.write_note("log.md", "Line 2", mode="append")

    content = (vault / "log.md").read_text(encoding="utf-8")
    assert content == "Line 1\nLine 2"


def test_markdown_write_overwrite(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "file.md").write_text("old data", encoding="utf-8")
    md = MarkdownVaultConnector(str(vault))

    res = md.write_note("file.md", "new data", mode="overwrite")
    assert res.committed is True

    content = (vault / "file.md").read_text(encoding="utf-8")
    assert content == "new data"


def test_markdown_write_frontmatter_roundtrip(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    md = MarkdownVaultConnector(str(vault))

    md.write_note(
        "doc",
        "Body text",
        frontmatter={"title": "Doc Title", "agent": "test"},
        mode="create",
    )

    note = md.read_note("doc.md")
    assert note["frontmatter"]["title"] == "Doc Title"
    assert note["frontmatter"]["agent"] == "test"
    assert note["content"] == "---\ntitle: Doc Title\nagent: test\n---\n\nBody text"


def test_markdown_write_sandbox_escape(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    md = MarkdownVaultConnector(str(vault))

    with pytest.raises(ValueError, match="Path traversal"):
        md.write_note("../evil", "hack", mode="create")


def test_writable_memory_protocol(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    md = MarkdownVaultConnector(str(vault))

    assert hasattr(md, "write_note")
    assert hasattr(md, "describe_write_side_effect")

    gbrain = GbrainConnector()
    assert hasattr(gbrain, "write_note")


def test_gbrain_write_uses_put_page(monkeypatch):
    monkeypatch.setenv("GBRAIN_TOKEN", "test-token")
    gbrain = GbrainConnector()

    with patch("urllib.request.urlopen") as mock_urlopen:
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({"result": {}}).encode("utf-8")
        mock_response.__enter__.return_value = mock_response
        mock_urlopen.return_value = mock_response

        res = gbrain.write_note("my-slug", "hello", mode="overwrite")

        assert res.committed is True
        assert res.ref == "agentos/my-slug"

        req = mock_urlopen.call_args[0][0]
        payload = json.loads(req.data.decode("utf-8"))
        assert payload["method"] == "tools/call"
        assert payload["params"]["name"] == "put_page"

        slug = payload["params"]["arguments"]["slug"]
        assert slug == "agentos/my-slug"

        content = payload["params"]["arguments"]["content"]
        assert "agent: agent-os" in content
        assert "hello" in content


def test_gbrain_write_prefixes_agentos(monkeypatch):
    monkeypatch.setenv("GBRAIN_TOKEN", "test-token")
    gbrain = GbrainConnector()

    with patch("urllib.request.urlopen") as mock_urlopen:
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({"result": {}}).encode("utf-8")
        mock_response.__enter__.return_value = mock_response
        mock_urlopen.return_value = mock_response

        res1 = gbrain.write_note("test1", "hello", mode="overwrite")
        assert res1.ref == "agentos/test1"

        res2 = gbrain.write_note("agentos/test2", "hello", mode="overwrite")
        assert res2.ref == "agentos/test2"


@pytest.mark.parametrize("mode", ("create", "append"))
def test_gbrain_rejects_modes_that_would_silently_upsert(monkeypatch, mode):
    monkeypatch.setenv("GBRAIN_TOKEN", "test-token")
    gbrain = GbrainConnector()

    with patch("urllib.request.urlopen") as mock_urlopen:
        with pytest.raises(
            NotImplementedError, match="only supports explicit overwrite"
        ):
            gbrain.write_note("existing-page", "replacement", mode=mode)

    mock_urlopen.assert_not_called()


@pytest.mark.integration
def test_gbrain_real_write_roundtrip():
    if not os.getenv("GBRAIN_TOKEN"):
        pytest.skip("GBRAIN_TOKEN not provided")

    gbrain = GbrainConnector()
    import uuid

    slug = f"agentos/test/r14c-smoke-{uuid.uuid4()}"

    try:
        res = gbrain.write_note(
            slug, "smoke body", frontmatter={"title": "r14c smoke"}, mode="overwrite"
        )
        assert res.committed is True
        assert res.ref == slug

        note = gbrain.read_note(slug)
        assert "smoke body" in note["content"]
        assert note["frontmatter"]["title"] == "r14c smoke"
        assert note["frontmatter"]["agent"] == "agent-os"

    finally:
        # Cleanup
        try:
            gbrain._call_rpc(
                "tools/call", {"name": "delete_page", "arguments": {"slug": slug}}
            )
        except Exception:
            pass
