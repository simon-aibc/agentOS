# Tutorial: Bring Your Own Memory Connector

This tutorial demonstrates how to build, test, and register a custom third-party memory connector for Agent OS using the stable `agent_os.api` surface and the `agent_os.testing` conformance kit.

---

## 1. Overview of Memory Protocols

Agent OS provides three memory protocols exported from `agent_os.api`:

1. **`MemoryConnector`** (Required): Read and search interface.
   - `search(query: str, limit: int = 10) -> list[dict]`
   - `read_note(ref: str) -> dict`
   - `list_notes() -> list[dict]`
2. **`WritableMemory`** (Optional): Write interface for modifying memory notes.
   - `supported_write_modes: frozenset[str]` (e.g., `{"create", "append", "overwrite"}`)
   - `write_note(ref: str, content: str, frontmatter: dict | None = None, mode: str = "create") -> MemoryWriteResult`
   - `describe_write_side_effect(ref: str, mode: str) -> str` (must return `"write"`)
3. **`IndexableMemory`** (Optional): Lifecycle interface for manual or automated indexing.
   - `index() -> IndexResult`
   - `reindex() -> IndexResult`
   - `index_status() -> dict`

---

## 2. Implementing a Custom Memory Connector

Here is an example in-memory SQLite-backed memory connector:

```python
# my_memory_package/connector.py
from __future__ import annotations

from typing import Any
from agent_os.api import (
    IndexResult,
    IndexableMemory,
    MemoryConnector,
    MemoryHit,
    MemoryWriteResult,
    WritableMemory,
)


class CustomMemoryConnector:
    """Example custom memory connector satisfying MemoryConnector, WritableMemory, and IndexableMemory."""

    name = "custom_memory"
    supported_write_modes = frozenset({"create", "append", "overwrite"})

    def __init__(self, storage_path: str = ":memory:") -> None:
        self.storage_path = storage_path
        self._notes: dict[str, str] = {}
        self._frontmatters: dict[str, dict[str, Any]] = {}

    # --- MemoryConnector Interface ---
    def list_notes(self) -> list[dict[str, Any]]:
        return [{"ref": ref, "title": ref} for ref in self._notes]

    def read_note(self, ref: str) -> dict[str, Any]:
        content = self._notes.get(ref, "")
        return {
            "ref": ref,
            "content": content,
            "frontmatter": self._frontmatters.get(ref, {}),
        }

    def search(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        if limit <= 0 or not query.strip():
            return []

        results = []
        for ref, content in self._notes.items():
            if query.lower() in content.lower() or query.lower() in ref.lower():
                results.append(
                    MemoryHit(
                        ref=ref,
                        snippet=content[:200],
                        score=1.0,
                        source={"connector": self.name},
                    ).model_dump()
                )
            if len(results) >= limit:
                break
        return results

    # --- WritableMemory Interface ---
    def describe_write_side_effect(self, ref: str, mode: str) -> str:
        return "write"

    def write_note(
        self,
        ref: str,
        content: str,
        frontmatter: dict[str, Any] | None = None,
        mode: str = "create",
    ) -> MemoryWriteResult:
        if mode not in self.supported_write_modes:
            raise ValueError(f"Unsupported write mode '{mode}'")

        if mode == "create" and ref in self._notes:
            raise FileExistsError(f"Note '{ref}' already exists")
        elif mode == "append" and ref in self._notes:
            self._notes[ref] = self._notes[ref] + "\n" + content
        else:
            self._notes[ref] = content

        if frontmatter:
            self._frontmatters[ref] = dict(frontmatter)

        return MemoryWriteResult(
            ref=ref,
            mode=mode,
            bytes_written=len(content.encode("utf-8")),
            committed=True,
        )

    # --- IndexableMemory Interface ---
    def index(self, refs: list[str] | None = None) -> IndexResult:
        count = len(refs) if refs is not None else len(self._notes)
        return IndexResult(indexed_count=count, deleted_count=0, errors=[])

    def reindex(self) -> IndexResult:
        return self.index()

    def index_status(self) -> dict[str, Any]:
        return {"status": "ready", "document_count": len(self._notes)}
```

---

## 3. Verifying Conformance with the Testing Kit

Agent OS provides a dependency-light conformance kit under `agent_os.testing`. Use it in your satellite package's pytest suite:

```python
# tests/test_my_connector.py
import pytest
from agent_os.testing import (
    check_memory_connector,
    check_indexable_memory,
    MemoryConnectorConformanceMixin,
)
from my_memory_package.connector import CustomMemoryConnector


def test_custom_memory_conformance():
    connector = CustomMemoryConnector()
    connector.write_note("welcome.md", "Hello world from custom memory", mode="create")

    # Run contract checks
    check_memory_connector(connector)
    check_indexable_memory(connector)


# Alternatively, use the pytest mixin:
class TestCustomMemory(MemoryConnectorConformanceMixin):
    @pytest.fixture
    def connector(self):
        inst = CustomMemoryConnector()
        inst.write_note("test.md", "Sample content", mode="create")
        return inst
```

---

## 4. Registering via Entry Points

Register your connector in your package's `pyproject.toml` so Agent OS can discover it automatically:

```toml
[project.entry-points."agent_os.memory_connectors"]
custom_memory = "my_memory_package.connector:CustomMemoryConnector"
```

> **Note on Name Protection**: Built-in connector names (`markdown`, `markdown_vault`, `gbrain`) are protected. If an external plugin declares a colliding name, Agent OS will fail closed on startup to prevent accidental shadowing.

---

## 5. Configuring in `workspace.toml`

Once installed in your Python environment, configure the workspace to use your connector:

```toml
[workspace]
name = "my-workspace"

[memory]
type = "custom_memory"
path = "./data"
```
