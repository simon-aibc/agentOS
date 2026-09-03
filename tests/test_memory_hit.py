from agent_os.connectors import MemoryHit, normalize_memory_hit
from skills.vault_qa.handlers import vault_qa


def test_memory_hit_model():
    hit = MemoryHit(
        ref="notes/ai.md",
        title="AI Notes",
        snippet="Machine learning notes",
        score=0.95,
        source={"author": "Simon", "url": "https://example.com/ai.md"},
    )
    assert hit.ref == "notes/ai.md"
    assert hit.title == "AI Notes"
    assert hit.snippet == "Machine learning notes"
    assert hit.score == 0.95
    assert hit.source == {"author": "Simon", "url": "https://example.com/ai.md"}


def test_normalize_memory_hit_from_hit_instance():
    hit = MemoryHit(ref="doc-1", snippet="sample text")
    normalized = normalize_memory_hit(hit)
    assert normalized is hit


def test_normalize_memory_hit_from_legacy_dict():
    raw = {
        "ref": "vault://notes/test.md",
        "snippet": "legacy snippet text",
        "author": "Alice",
        "created_at": "2026-08-21",
    }
    normalized = normalize_memory_hit(raw)
    assert normalized.ref == "vault://notes/test.md"
    assert normalized.snippet == "legacy snippet text"
    assert normalized.source == {"author": "Alice", "created_at": "2026-08-21"}


def test_normalize_memory_hit_from_minimal_dict():
    raw = {"path": "raw_path.md"}
    normalized = normalize_memory_hit(raw)
    assert normalized.ref == "raw_path.md"
    assert normalized.snippet == ""
    assert normalized.source == {}


def test_normalize_memory_hit_from_string():
    normalized = normalize_memory_hit("plain_text_reference")
    assert normalized.ref == "plain_text_reference"
    assert normalized.snippet == "plain_text_reference"


def test_vault_qa_renders_provenance_when_source_present():
    class MemoryWithProvenance:
        name = "prov_mem"

        def search(self, query: str, limit: int = 3):
            return [
                MemoryHit(
                    ref="arxiv:2401.0001",
                    snippet="State-of-the-art multi-agent systems",
                    source={"author": "Tran et al.", "year": 2026},
                )
            ]

        def read_note(self, ref: str):
            return {"ref": ref, "content": "Full text"}

    result = vault_qa("hỏi vault: multi-agent", connector=MemoryWithProvenance())
    assert "[arxiv:2401.0001]" in result
    assert "Trích đoạn: State-of-the-art multi-agent systems..." in result
    assert "Nguồn: author=Tran et al., year=2026" in result


def test_vault_qa_legacy_format_when_source_empty():
    class LegacyMemory:
        name = "legacy_mem"

        def search(self, query: str, limit: int = 3):
            return [{"ref": "note1.md", "snippet": "simple snippet"}]

        def read_note(self, ref: str):
            return {"ref": ref, "content": "content"}

    result = vault_qa("hỏi vault: query", connector=LegacyMemory())
    assert "[note1.md]" in result
    assert "Trích đoạn: simple snippet...\n" in result
    assert "Nguồn:" not in result
