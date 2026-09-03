from pathlib import Path

from agent_os.connectors import IndexableMemory, IndexResult, MarkdownVaultConnector


def test_markdown_vault_connector_implements_indexable_memory(tmp_path: Path):
    connector = MarkdownVaultConnector(str(tmp_path))
    assert isinstance(connector, IndexableMemory)
    assert callable(connector.index)
    assert callable(connector.reindex)
    assert callable(connector.index_status)


def test_markdown_vault_bm25_indexing_and_ranking(tmp_path: Path):
    vault = MarkdownVaultConnector(str(tmp_path))

    # Create notes
    vault.write_note(
        "note1.md",
        "# Python Architecture\n\nPython architecture and multi-agent coordination system.",
    )
    vault.write_note(
        "note2.md",
        "# Database Design\n\nPostgres schema design and relational database systems.",
    )
    vault.write_note(
        "note3.md",
        "# Python Deep Dive\n\nPython Python Python programming language internals.",
    )

    # Build index
    res = vault.index()
    assert isinstance(res, IndexResult)
    assert res.indexed_count == 3
    assert res.deleted_count == 0
    assert not res.errors

    # Search for 'python': note3 has higher frequency -> higher rank
    hits = vault.search("Python")
    assert len(hits) == 2
    assert hits[0]["ref"] == "note3.md"
    assert hits[1]["ref"] == "note1.md"
    assert hits[0]["score"] > hits[1]["score"]
    assert "source" in hits[0]
    assert hits[0]["source"]["path"] == "note3.md"


def test_match_centered_snippet_and_heading_provenance(tmp_path: Path):
    vault = MarkdownVaultConnector(str(tmp_path))
    long_prefix = "Lorem ipsum " * 40
    long_suffix = " dolor sit amet" * 40
    content = f"# System Specification\n\n{long_prefix}\n\n## Security Model\n\n{long_prefix}ANTIGRAVITY_CORE_AGENT{long_suffix}"
    vault.write_note("spec.md", content)

    vault.index()
    hits = vault.search("ANTIGRAVITY_CORE_AGENT")
    assert len(hits) == 1
    hit = hits[0]

    assert "ANTIGRAVITY_CORE_AGENT" in hit["snippet"]
    assert hit["snippet"].startswith("...")
    assert hit["snippet"].endswith("...")
    assert hit["source"]["heading"] == "Security Model"


def test_index_excludes_hidden_and_internal_files(tmp_path: Path):
    vault = MarkdownVaultConnector(str(tmp_path))
    vault.write_note("valid.md", "# Valid Note\n\nRegular note content.")

    # Create hidden note and non-markdown file
    hidden_dir = tmp_path / ".hidden"
    hidden_dir.mkdir()
    (hidden_dir / "secret.md").write_text("# Secret\n\nHidden", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("Text file", encoding="utf-8")

    res = vault.index()
    assert res.indexed_count == 1

    status = vault.index_status()
    assert status["document_count"] == 1
    assert status["status"] == "ready"


def test_reindex_resets_and_rebuilds(tmp_path: Path):
    vault = MarkdownVaultConnector(str(tmp_path))
    vault.write_note("a.md", "# Alpha\n\nContent A")
    vault.index()

    vault.write_note("b.md", "# Beta\n\nContent B")
    res = vault.reindex()
    assert res.indexed_count == 2
    assert vault.index_status()["document_count"] == 2


def test_index_status_detects_stale_notes(tmp_path: Path):
    vault = MarkdownVaultConnector(str(tmp_path))
    vault.write_note("doc.md", "# Doc\n\nInitial version")
    vault.index()

    status1 = vault.index_status()
    assert status1["status"] == "ready"

    # Modify file after index
    (tmp_path / "doc.md").write_text("# Doc\n\nModified content", encoding="utf-8")
    status2 = vault.index_status()
    assert status2["status"] == "stale"


def test_cold_start_large_unindexed_vault_does_not_unbounded_index(tmp_path: Path):
    vault = MarkdownVaultConnector(str(tmp_path))
    # Create 55 notes (> MAX_AUTO_INDEX_FILES=50)
    for i in range(55):
        (tmp_path / f"note_{i:03d}.md").write_text(
            f"# Note {i}\n\nSearch keyword target in note {i}.", encoding="utf-8"
        )

    assert not (tmp_path / ".agentos_vault_index.json").exists()

    # Search on unindexed large vault must NOT trigger unbounded build and safely return []
    hits = vault.search("target")
    assert hits == []
    assert not (tmp_path / ".agentos_vault_index.json").exists()
    assert vault.index_status()["status"] == "unindexed"

    # Explicit index command builds index
    res = vault.index()
    assert res.indexed_count == 55
    assert (tmp_path / ".agentos_vault_index.json").exists()

    hits_after = vault.search("target")
    assert len(hits_after) == 10
    assert hits_after[0]["score"] > 0.0


def test_cold_start_small_unindexed_vault_auto_builds_safely(tmp_path: Path):
    vault = MarkdownVaultConnector(str(tmp_path))
    for i in range(3):
        (tmp_path / f"small_{i}.md").write_text(
            f"# Small {i}\n\nTarget content {i}", encoding="utf-8"
        )

    # Search on small vault (< 50 files) safely auto-builds index
    hits = vault.search("Target")
    assert len(hits) == 3
    assert (tmp_path / ".agentos_vault_index.json").exists()
