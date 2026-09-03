import datetime as dt
import json
import math
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from agent_os.sandbox import resolve_sandbox_path
from agent_os.schemas import ExecutionResult


class MemoryHit(BaseModel):
    """Typed representation of a memory search or retrieval hit."""

    ref: str
    title: str | None = None
    snippet: str = ""
    score: float | None = None
    source: dict[str, Any] = Field(default_factory=dict)


def normalize_memory_hit(hit: dict[str, Any] | MemoryHit) -> MemoryHit:
    """Normalize a memory search result dict or MemoryHit into a canonical MemoryHit."""
    if isinstance(hit, MemoryHit):
        return hit
    if not isinstance(hit, dict):
        return MemoryHit(ref=str(hit), snippet=str(hit))

    ref = str(hit.get("ref", hit.get("path", hit.get("slug", "unknown"))))
    title = hit.get("title")
    if title is not None:
        title = str(title)
    snippet = str(hit.get("snippet", hit.get("content", "")))
    score = hit.get("score")
    if score is not None:
        try:
            score = float(score)
        except (ValueError, TypeError):
            score = None

    source = hit.get("source")
    if isinstance(source, dict):
        source_dict = dict(source)
    else:
        known_keys = {
            "ref",
            "path",
            "slug",
            "title",
            "snippet",
            "content",
            "score",
            "source",
        }
        source_dict = {k: v for k, v in hit.items() if k not in known_keys}

    return MemoryHit(
        ref=ref,
        title=title,
        snippet=snippet,
        score=score,
        source=source_dict,
    )


class Connector(Protocol):
    """
    A Connector is a tool wrapper that encapsulates logic and context for a specific domain.
    """

    @property
    def name(self) -> str: ...

    def capabilities(self) -> dict[str, Any]: ...

    def describe_side_effect(self, action: str) -> str: ...

    def invoke(self, action: str, args: dict[str, Any]) -> ExecutionResult: ...


class MemoryConnector(Protocol):
    """
    A MemoryConnector provides reading capabilities to search, read, and list notes.
    """

    @property
    def name(self) -> str: ...

    def search(self, query: str, limit: int = 10) -> list[dict[str, Any]]: ...

    def read_note(self, slug_or_path: str) -> dict[str, Any]: ...

    def list_notes(
        self, filters: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]: ...


@dataclass
class MemoryWriteResult:
    ref: str
    mode: str  # "create" | "append" | "overwrite"
    bytes_written: int | None
    committed: bool
    # A policy rejection is not an I/O exception, but callers still need a
    # machine-readable way to distinguish it from a successful write.  Native
    # connectors leave these defaults intact; the memory gate fills them from
    # the policy execution result when a write never reaches the connector.
    status: Literal["completed", "failed", "cancelled"] = "completed"
    error: str | None = None


class IndexResult(BaseModel):
    """Result of an index or reindex operation."""

    indexed_count: int
    deleted_count: int = 0
    errors: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


@runtime_checkable
class IndexableMemory(Protocol):
    """A memory connector supporting optional indexing lifecycle operations."""

    @property
    def name(self) -> str: ...

    def index(self, refs: list[str] | None = None) -> IndexResult: ...

    def reindex(self) -> IndexResult: ...

    def index_status(self) -> dict[str, Any]: ...


class WritableMemory(Protocol):
    @property
    def name(self) -> str: ...
    @property
    def supported_write_modes(self) -> frozenset[str]: ...
    def write_note(
        self,
        ref: str,
        content: str,
        frontmatter: dict[str, Any] | None = None,
        mode: Literal["create", "append", "overwrite"] = "create",
    ) -> MemoryWriteResult: ...
    def describe_write_side_effect(self, ref: str, mode: str) -> str: ...


class ConnectorRegistry:
    def __init__(self) -> None:
        self._connectors: dict[str, Any] = {}

    def register(self, name: str, connector: Any) -> None:
        if name in self._connectors:
            raise ValueError(f"Connector '{name}' is already registered.")
        self._connectors[name] = connector

    def resolve(self, name: str) -> Any:
        if name not in self._connectors:
            raise ValueError(f"Connector '{name}' not found in registry.")
        return self._connectors[name]

    def list_connectors(self) -> list[str]:
        return list(self._connectors.keys())


class FilesystemConnector(Connector):
    @property
    def name(self) -> str:
        return "filesystem"

    def capabilities(self) -> dict[str, Any]:
        return {"actions": ["read", "list", "stat"]}

    def describe_side_effect(self, action: str) -> str:
        return "read"

    def invoke(self, action: str, args: dict[str, Any]) -> ExecutionResult:
        if action not in ["read", "list", "stat"]:
            return ExecutionResult(
                status="failed", errors=[f"Unsupported action: {action}"]
            )

        path = args.get("path")
        if not path:
            return ExecutionResult(status="failed", errors=["Missing argument: path"])

        try:
            resolved = resolve_sandbox_path(path)
        except ValueError as e:
            return ExecutionResult(status="failed", errors=[str(e)])

        try:
            if action == "read":
                content = resolved.read_text(encoding="utf-8")
                return ExecutionResult(status="completed", outputs={"content": content})
            elif action == "list":
                items = [p.name for p in resolved.iterdir()]
                return ExecutionResult(status="completed", outputs={"items": items})
            elif action == "stat":
                st = resolved.stat()
                return ExecutionResult(
                    status="completed",
                    outputs={"size": st.st_size, "is_dir": resolved.is_dir()},
                )
        except Exception as e:
            return ExecutionResult(
                status="failed", errors=[f"Filesystem error: {str(e)}"]
            )

        return ExecutionResult(status="failed", errors=["Unknown error"])


def _parse_frontmatter(content: str) -> dict[str, Any]:
    match = re.match(r"^---\n(.*?)\n---", content, re.DOTALL)
    if not match:
        return {}
    fm = match.group(1)
    res = {}
    for line in fm.split("\n"):
        if ":" in line:
            k, v = line.split(":", 1)
            res[k.strip()] = v.strip()
    return res


def _serialize_markdown_with_frontmatter(
    content: str, frontmatter: dict[str, Any]
) -> str:
    if not frontmatter:
        return content
    fm_lines = ["---"]
    for k, v in frontmatter.items():
        fm_lines.append(f"{k}: {v}")
    fm_lines.append("---")
    fm_lines.append("")
    fm_lines.append(content)
    return "\n".join(fm_lines)


def _is_indexable_note_path(path: Path, root: Path) -> bool:
    """Check if a path is an indexable markdown note (excluding hidden/agentos files)."""
    try:
        rel = path.relative_to(root)
    except ValueError:
        return False
    for part in rel.parts:
        if part.startswith("."):
            return False
    return path.suffix.lower() == ".md"


def _tokenize(text: str) -> list[str]:
    return [t for t in re.findall(r"\w+", text.lower()) if t]


def _extract_headings(content: str) -> list[dict[str, Any]]:
    headings = []
    for match in re.finditer(r"^(#{1,6})\s+(.+)$", content, re.MULTILINE):
        headings.append(
            {
                "level": len(match.group(1)),
                "heading": match.group(2).strip(),
                "start": match.start(),
                "end": match.end(),
            }
        )
    return headings


def _generate_match_snippet(
    content: str, query_tokens: list[str], max_len: int = 200
) -> tuple[str, str | None]:
    headings = _extract_headings(content)
    if not query_tokens:
        snippet = content[:max_len].strip()
        heading = headings[0]["heading"] if headings else None
        return snippet, heading

    lower = content.lower()
    best_pos = -1
    for token in query_tokens:
        pos = lower.find(token)
        if pos != -1 and (best_pos == -1 or pos < best_pos):
            best_pos = pos

    if best_pos == -1:
        snippet = content[:max_len].strip()
        heading = headings[0]["heading"] if headings else None
        return snippet, heading

    half = max_len // 2
    start = max(0, best_pos - half)
    end = min(len(content), best_pos + half)
    snippet = content[start:end].strip()
    if start > 0:
        snippet = "..." + snippet
    if end < len(content):
        snippet = snippet + "..."

    heading = None
    for h in headings:
        if h["start"] <= best_pos:
            heading = h["heading"]
        else:
            break
    if not heading and headings:
        heading = headings[0]["heading"]

    return snippet, heading


MAX_AUTO_INDEX_FILES = 50
MAX_AUTO_INDEX_BYTES = 1_000_000


def _should_auto_build_index(root_path: Path) -> bool:
    """Check if unindexed vault is within safe auto-build limits."""
    note_count = 0
    total_bytes = 0
    for p in root_path.rglob("*.md"):
        if _is_indexable_note_path(p, root_path):
            note_count += 1
            if note_count > MAX_AUTO_INDEX_FILES:
                return False
            try:
                total_bytes += p.stat().st_size
                if total_bytes > MAX_AUTO_INDEX_BYTES:
                    return False
            except OSError:
                continue
    return True


class MarkdownVaultConnector(MemoryConnector):
    supported_write_modes = frozenset({"append", "create", "overwrite"})

    def __init__(self, root_path: str):
        self.root_path = Path(root_path).resolve()

    @property
    def name(self) -> str:
        return "markdown_vault"

    def _index_path(self) -> Path:
        return self.root_path / ".agentos_vault_index.json"

    def describe_write_side_effect(self, ref: str, mode: str) -> str:
        if mode == "append":
            return f"append to note '{ref}' (creates if missing)"
        elif mode == "overwrite":
            return f"OVERWRITE note '{ref}' — existing content lost"
        return f"create note '{ref}'"

    def write_note(
        self,
        ref: str,
        content: str,
        frontmatter: dict[str, Any] | None = None,
        mode: Literal["create", "append", "overwrite"] = "create",
    ) -> MemoryWriteResult:
        path = self.root_path / ref
        if not path.name.endswith(".md"):
            path = path.with_suffix(".md")

        try:
            path = path.resolve()
            path.relative_to(self.root_path)
        except ValueError as e:
            raise ValueError(f"Path traversal detected: {ref}") from e

        path.parent.mkdir(parents=True, exist_ok=True)

        if mode == "create" and path.exists():
            raise FileExistsError(f"Note already exists: {ref}")

        final_content = content
        if mode in ("create", "overwrite") and frontmatter:
            final_content = _serialize_markdown_with_frontmatter(content, frontmatter)

        if mode == "append":
            file_exists = path.exists() and path.stat().st_size > 0
            with open(path, "a", encoding="utf-8") as f:
                if file_exists:
                    bytes_written = f.write("\n" + final_content)
                else:
                    bytes_written = f.write(final_content)
        else:
            with open(path, "w", encoding="utf-8") as f:
                bytes_written = f.write(final_content)

        return MemoryWriteResult(
            ref=path.relative_to(self.root_path).as_posix(),
            mode=mode,
            bytes_written=bytes_written,
            committed=True,
        )

    def _load_or_build_index(self) -> dict[str, Any]:
        idx_path = self._index_path()
        if idx_path.is_file():
            try:
                with idx_path.open("r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, dict) and "docs" in data:
                        return data
            except Exception:
                pass

        # Cold-start guard: only auto-build if vault is within bounded limits.
        # Otherwise, return empty unindexed data without performing unbounded full scan.
        # `agent-os memory index` remains the authoritative full rebuild path.
        if _should_auto_build_index(self.root_path):
            self.index()
            if idx_path.is_file():
                try:
                    with idx_path.open("r", encoding="utf-8") as f:
                        return json.load(f)
                except Exception:
                    pass

        return {"version": 1, "doc_count": 0, "avg_doc_len": 0.0, "docs": {}}

    def search(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        query_tokens = _tokenize(query)
        if not query_tokens:
            return []

        index_data = self._load_or_build_index()
        docs = index_data.get("docs", {})
        doc_count = len(docs)
        if doc_count == 0:
            return []

        avg_doc_len = float(index_data.get("avg_doc_len", 1.0) or 1.0)
        idfs = {}
        for token in set(query_tokens):
            n_q = sum(1 for d in docs.values() if token in d.get("term_freqs", {}))
            idfs[token] = math.log((doc_count - n_q + 0.5) / (n_q + 0.5) + 1.0)

        k1 = 1.5
        b = 0.75
        scored = []
        for ref, d in docs.items():
            doc_len = d.get("doc_len", 0)
            tf_dict = d.get("term_freqs", {})
            score = 0.0
            for token in query_tokens:
                tf = tf_dict.get(token, 0)
                if tf > 0:
                    idf = idfs.get(token, 0.0)
                    denom = tf + k1 * (1.0 - b + b * (doc_len / avg_doc_len))
                    score += idf * (tf * (k1 + 1.0)) / denom
            if score > 0.0:
                scored.append((score, ref, d.get("title")))

        scored.sort(key=lambda x: x[0], reverse=True)
        results = []
        for score, ref, title in scored[:limit]:
            note_path = self.root_path / ref
            try:
                content = note_path.read_text(encoding="utf-8")
                snippet, heading = _generate_match_snippet(content, query_tokens)
            except Exception:
                snippet = ""
                heading = None

            source = {"path": ref, "source_type": "markdown_vault"}
            if heading:
                source["heading"] = heading

            results.append(
                {
                    "ref": ref,
                    "title": title,
                    "snippet": snippet,
                    "score": round(score, 4),
                    "source": source,
                }
            )
        return results

    def read_note(self, ref: str) -> dict[str, Any]:
        path = self.root_path / ref
        if not path.name.endswith(".md"):
            path = path.with_suffix(".md")

        try:
            content = path.read_text(encoding="utf-8")
            fm = _parse_frontmatter(content)

            # Follow basic wikilinks `[[Link]]`
            links = re.findall(r"\[\[(.*?)\]\]", content)

            return {"ref": ref, "content": content, "frontmatter": fm, "links": links}
        except FileNotFoundError as e:
            raise ValueError(f"Note not found: {ref}") from e

    def list_notes(
        self, filters: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        results = []
        for p in self.root_path.rglob("*.md"):
            if not _is_indexable_note_path(p, self.root_path):
                continue
            try:
                content = p.read_text(encoding="utf-8")
                fm = _parse_frontmatter(content)
                title = fm.get("title")
                if not title:
                    h1_match = re.search(r"^#\s+(.+)$", content, re.MULTILINE)
                    title = h1_match.group(1).strip() if h1_match else None
            except Exception:
                title = None
            results.append(
                {"ref": p.relative_to(self.root_path).as_posix(), "title": title}
            )
        return results

    def index(self, refs: list[str] | None = None) -> IndexResult:
        idx_path = self._index_path()
        index_data = {}
        if idx_path.is_file():
            try:
                with idx_path.open("r", encoding="utf-8") as f:
                    index_data = json.load(f)
            except Exception:
                index_data = {}

        docs = index_data.get("docs", {})
        errors: list[str] = []
        indexed_count = 0
        deleted_count = 0

        if refs is not None:
            for ref in refs:
                note_path = self.root_path / ref
                if not note_path.name.endswith(".md"):
                    note_path = note_path.with_suffix(".md")
                rel_posix = note_path.relative_to(self.root_path).as_posix()
                if not note_path.is_file() or not _is_indexable_note_path(
                    note_path, self.root_path
                ):
                    if rel_posix in docs:
                        del docs[rel_posix]
                        deleted_count += 1
                    continue
                try:
                    content = note_path.read_text(encoding="utf-8")
                    tokens = _tokenize(content)
                    tf: dict[str, int] = {}
                    for t in tokens:
                        tf[t] = tf.get(t, 0) + 1
                    fm = _parse_frontmatter(content)
                    title = fm.get("title")
                    if not title:
                        h1_match = re.search(r"^#\s+(.+)$", content, re.MULTILINE)
                        title = h1_match.group(1).strip() if h1_match else None
                    docs[rel_posix] = {
                        "mtime": note_path.stat().st_mtime,
                        "doc_len": len(tokens),
                        "title": title,
                        "term_freqs": tf,
                    }
                    indexed_count += 1
                except Exception as exc:
                    errors.append(f"Failed to index {ref}: {exc}")
        else:
            found_refs = set()
            for p in self.root_path.rglob("*.md"):
                if not _is_indexable_note_path(p, self.root_path):
                    continue
                rel_posix = p.relative_to(self.root_path).as_posix()
                found_refs.add(rel_posix)
                try:
                    content = p.read_text(encoding="utf-8")
                    tokens = _tokenize(content)
                    tf = {}
                    for t in tokens:
                        tf[t] = tf.get(t, 0) + 1
                    fm = _parse_frontmatter(content)
                    title = fm.get("title")
                    if not title:
                        h1_match = re.search(r"^#\s+(.+)$", content, re.MULTILINE)
                        title = h1_match.group(1).strip() if h1_match else None
                    docs[rel_posix] = {
                        "mtime": p.stat().st_mtime,
                        "doc_len": len(tokens),
                        "title": title,
                        "term_freqs": tf,
                    }
                    indexed_count += 1
                except Exception as exc:
                    errors.append(f"Failed to index {rel_posix}: {exc}")

            existing_doc_keys = list(docs.keys())
            for k in existing_doc_keys:
                if k not in found_refs:
                    del docs[k]
                    deleted_count += 1

        total_tokens = sum(d.get("doc_len", 0) for d in docs.values())
        avg_doc_len = (total_tokens / len(docs)) if docs else 0.0

        new_index = {
            "version": 1,
            "updated_at": dt.datetime.now(dt.UTC).isoformat(),
            "doc_count": len(docs),
            "avg_doc_len": round(avg_doc_len, 2),
            "docs": docs,
        }
        try:
            with idx_path.open("w", encoding="utf-8") as f:
                json.dump(new_index, f, indent=2)
        except Exception as exc:
            errors.append(f"Failed to save index file: {exc}")

        return IndexResult(
            indexed_count=indexed_count,
            deleted_count=deleted_count,
            errors=errors,
            metadata={"doc_count": len(docs), "avg_doc_len": round(avg_doc_len, 2)},
        )

    def reindex(self) -> IndexResult:
        idx_path = self._index_path()
        if idx_path.is_file():
            try:
                idx_path.unlink()
            except Exception:
                pass
        return self.index()

    def index_status(self) -> dict[str, Any]:
        idx_path = self._index_path()
        if not idx_path.is_file():
            return {
                "status": "unindexed",
                "document_count": 0,
                "index_path": str(idx_path),
                "last_indexed_at": None,
            }
        try:
            with idx_path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            docs = data.get("docs", {})
            is_stale = False
            found_count = 0
            for p in self.root_path.rglob("*.md"):
                if not _is_indexable_note_path(p, self.root_path):
                    continue
                found_count += 1
                rel_posix = p.relative_to(self.root_path).as_posix()
                if rel_posix not in docs or p.stat().st_mtime > docs[
                    rel_posix
                ].get("mtime", 0):
                    is_stale = True
                    break
            if not is_stale and found_count != len(docs):
                is_stale = True

            return {
                "status": "stale" if is_stale else "ready",
                "document_count": len(docs),
                "index_path": str(idx_path),
                "last_indexed_at": data.get("updated_at"),
            }
        except Exception:
            return {
                "status": "error",
                "document_count": 0,
                "index_path": str(idx_path),
                "last_indexed_at": None,
            }


class GbrainConnector(MemoryConnector):
    # Gbrain currently exposes only ``put_page``, an unconditional upsert.
    # Do not map the safer create/append vocabulary onto that destructive
    # primitive; callers must explicitly request overwrite instead.
    supported_write_modes = frozenset({"overwrite"})

    def __init__(self):
        self.url = os.getenv("GBRAIN_URL", "http://localhost:3131/mcp")
        self.token = os.getenv("GBRAIN_TOKEN")

    @property
    def name(self) -> str:
        return "gbrain"

    def _call_rpc(self, method: str, params: dict[str, Any]) -> Any:
        if not self.token:
            raise RuntimeError("gbrain not configured")

        payload = json.dumps(
            {"jsonrpc": "2.0", "method": method, "params": params, "id": 1}
        ).encode("utf-8")

        req = urllib.request.Request(
            self.url,
            data=payload,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=15) as response:
                content = response.read().decode("utf-8")
                # Parse SSE if applicable
                for line in content.split("\n"):
                    if line.startswith("data: "):
                        try:
                            data = json.loads(line[6:])
                            if "result" in data:
                                return data["result"]
                        except json.JSONDecodeError:
                            pass

                # Direct JSON fallback
                try:
                    data = json.loads(content)
                    if "result" in data:
                        return data["result"]
                except json.JSONDecodeError:
                    pass
                return content
        except urllib.error.URLError as e:
            raise RuntimeError(f"Gbrain error: {e}") from e

    def search(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        res = self._call_rpc(
            "tools/call",
            {"name": "query", "arguments": {"query": query, "limit": limit}},
        )

        hits = []
        if isinstance(res, dict):
            c_list = res.get("content") or []
            if c_list and isinstance(c_list, list):
                text = c_list[0].get("text", "")
                try:
                    import json

                    parsed = json.loads(text)
                    if isinstance(parsed, dict) and "hits" in parsed:
                        hits = parsed["hits"]
                    elif isinstance(parsed, list):
                        hits = parsed
                except (json.JSONDecodeError, TypeError):
                    pass
        elif isinstance(res, list):
            hits = res

        results = []
        if isinstance(hits, list):
            for hit in hits:
                if isinstance(hit, dict):
                    results.append(
                        {
                            "ref": hit.get("slug", ""),
                            "title": hit.get("title"),
                            "snippet": hit.get("chunk_text", "")[:200],
                            "score": hit.get("score"),
                        }
                    )
        return results

    def read_note(self, ref: str) -> dict[str, Any]:
        res = self._call_rpc(
            "tools/call", {"name": "get_page", "arguments": {"slug": ref}}
        )

        content = ""
        fm: dict[str, Any] = {}
        if isinstance(res, dict):
            c_list = res.get("content") or []
            if c_list and isinstance(c_list, list):
                text = c_list[0].get("text", "")
                try:
                    parsed = json.loads(text)
                    if isinstance(parsed, dict):
                        content = parsed.get("compiled_truth") or parsed.get(
                            "content", ""
                        )
                        # gbrain hoists `title` and stores remaining frontmatter keys
                        # as a top-level `frontmatter` dict; compiled_truth is body-only,
                        # so parse from those fields, not from the (stripped) body.
                        fm = dict(parsed.get("frontmatter") or {})
                        if parsed.get("title"):
                            fm.setdefault("title", parsed["title"])
                    else:
                        content = text
                except (json.JSONDecodeError, TypeError):
                    content = text
        elif isinstance(res, str):
            content = res

        if not fm:
            # Fallback: raw markdown body (non-gbrain-shaped response)
            fm = _parse_frontmatter(content)
        links = re.findall(r"\[\[(.*?)\]\]", content)

        return {"ref": ref, "content": content, "frontmatter": fm, "links": links}

    def list_notes(self, filters: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        res = self._call_rpc(
            "tools/call", {"name": "list_pages", "arguments": filters or {}}
        )

        pages = []
        if isinstance(res, dict):
            c_list = res.get("content") or []
            if c_list and isinstance(c_list, list):
                text = c_list[0].get("text", "")
                try:
                    import json

                    parsed = json.loads(text)
                    if isinstance(parsed, dict) and "pages" in parsed:
                        pages = parsed["pages"]
                    elif isinstance(parsed, list):
                        pages = parsed
                except (json.JSONDecodeError, TypeError):
                    pass
        elif isinstance(res, list):
            pages = res

        results = []
        if isinstance(pages, list):
            for page in pages:
                if isinstance(page, dict):
                    results.append(
                        {"ref": page.get("slug", ""), "title": page.get("title")}
                    )
        return results

    def describe_write_side_effect(self, ref: str, mode: str) -> str:
        if mode == "overwrite":
            return f"OVERWRITE gbrain page '{ref}' via put_page upsert"
        return (
            f"Gbrain does not support safe {mode} for '{ref}'; "
            "its available write primitive is an upsert"
        )

    def write_note(
        self,
        ref: str,
        content: str,
        frontmatter: dict[str, Any] | None = None,
        mode: Literal["create", "append", "overwrite"] = "create",
    ) -> MemoryWriteResult:
        if mode != "overwrite":
            raise NotImplementedError(
                "GbrainConnector only supports explicit overwrite: its put_page "
                "operation is an upsert and cannot safely implement create or append."
            )

        if not ref.startswith("agentos/"):
            ref = f"agentos/{ref}"

        merged_fm = {}
        if frontmatter:
            merged_fm.update(frontmatter)

        import datetime

        now = datetime.datetime.now(datetime.UTC).isoformat()

        merged_fm.setdefault("agent", "agent-os")
        merged_fm.setdefault("created", now)
        merged_fm.setdefault("via", "agent-os-v1.4")
        merged_fm.setdefault("source", "default")

        final_content = _serialize_markdown_with_frontmatter(content, merged_fm)

        self._call_rpc(
            "tools/call",
            {"name": "put_page", "arguments": {"slug": ref, "content": final_content}},
        )

        return MemoryWriteResult(
            ref=ref,
            mode=mode,
            bytes_written=len(final_content.encode("utf-8")),
            committed=True,
        )
