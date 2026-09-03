import re
from pathlib import Path
from typing import Any, Literal

from agent_os.api import (
    ActionProposal,
    AuthStatus,
    BackendRole,
    ContextBlock,
    ExecutionResult,
    IndexResult,
    MemoryHit,
    MemoryWriteResult,
    PolicyDecision,
    Principal,
)
from agent_os.testing import (
    check_backend_adapter,
    check_connector,
    check_context_provider,
    check_event_sink,
    check_indexable_memory,
    check_memory_connector,
)


def test_extending_docs_snippets():
    # 1. NotesMemory
    class NotesMemory:
        name = "notes"

        def search(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
            return [
                MemoryHit(ref="welcome.md", snippet=query, score=1.0).model_dump()
            ][:limit]

        def read_note(self, ref: str) -> dict[str, Any]:
            return {"ref": ref, "content": "Welcome note"}

        def list_notes(self, filters: dict[str, Any] | None = None) -> list[dict[str, Any]]:
            return [{"ref": "welcome.md", "title": "Welcome"}]

    check_memory_connector(NotesMemory())

    # 2. WritableNotesMemory
    class WritableNotesMemory:
        name = "writable_notes"
        supported_write_modes = frozenset({"create", "append", "overwrite"})

        def describe_write_side_effect(self, ref: str, mode: str) -> str:
            return "write"

        def write_note(
            self,
            ref: str,
            content: str,
            frontmatter: dict[str, Any] | None = None,
            mode: Literal["create", "append", "overwrite"] = "create",
        ) -> MemoryWriteResult:
            return MemoryWriteResult(
                ref=ref,
                mode=mode,
                bytes_written=len(content.encode("utf-8")),
                committed=True,
            )

    wm = WritableNotesMemory()
    res = wm.write_note("test.md", "content")
    assert res.committed is True
    assert wm.describe_write_side_effect("test.md", "create") == "write"

    # 3. CustomActionConnector
    class CustomActionConnector:
        name = "ops_tools"

        def capabilities(self) -> dict[str, Any]:
            return {"actions": ["fetch_metrics", "deploy_service"]}

        def describe_side_effect(self, action: str) -> str:
            if action == "fetch_metrics":
                return "read"
            return "write"

        def invoke(self, action: str, args: dict[str, Any]) -> ExecutionResult:
            return ExecutionResult(status="completed", outputs={"status": "ok"})

    check_connector(CustomActionConnector())

    # 4. SchemaContextProvider
    class SchemaContextProvider:
        name = "db_schema"

        def provide(
            self,
            task: str,
            *,
            budget_chars: int,
            principal: Principal,
            workspace_id: str,
        ) -> list[ContextBlock]:
            return [
                ContextBlock(
                    source="db_schema",
                    content="TABLE users (id INT, email TEXT);",
                )
            ]

    check_context_provider(SchemaContextProvider())

    # 5. CustomLLMBackend
    class CustomLLMBackend:
        name = "custom_llm"
        binary_name = "custom-agent"
        supported_roles = frozenset({"executor"})
        stub = False

        def authentication_status(self) -> AuthStatus:
            return AuthStatus(status="ok", detail="API key verified")

        def build_invoker(self, role: BackendRole):
            if role not in self.supported_roles:
                raise ValueError(f"Unsupported role: {role}")

            def invoke(state):
                return ExecutionResult(status="completed", outputs={"result": "done"})

            return invoke

    check_backend_adapter(CustomLLMBackend())

    # 6. ConsoleEventSink
    class ConsoleEventSink:
        name = "console_audit"

        def emit(self, event: dict[str, Any]) -> None:
            pass

    check_event_sink(ConsoleEventSink())

    # 7. CustomReadOnlyPolicy
    class CustomReadOnlyPolicy:
        def evaluate(
            self,
            proposal: ActionProposal,
            *,
            workspace: Any = None,
            context: Any = None,
        ) -> PolicyDecision:
            decision = "allow" if proposal.side_effect in {"none", "read"} else "require_approval"
            return PolicyDecision(decision=decision, policy_id="custom-read-only")

    pol = CustomReadOnlyPolicy()
    p_dec = pol.evaluate(ActionProposal(tool="read_file", side_effect="read"))
    assert p_dec.decision == "allow"


def test_readme_connector_snippet():
    readme = Path(__file__).resolve().parents[1] / "README.md"
    content = readme.read_text(encoding="utf-8")
    match = re.search(
        r"### Authoring an Agent OS Plugin\n.*?```python\n(.*?)```",
        content,
        flags=re.DOTALL,
    )
    assert match is not None, "README connector snippet is missing"

    namespace: dict[str, Any] = {}
    exec(match.group(1), namespace)
    check_connector(namespace["MyCustomConnector"]())


def test_bring_your_own_memory_tutorial_snippet():

    class CustomMemoryConnector:
        name = "custom_memory"
        supported_write_modes = frozenset({"create", "append", "overwrite"})

        def __init__(self, storage_path: str = ":memory:") -> None:
            self.storage_path = storage_path
            self._notes: dict[str, str] = {}
            self._frontmatters: dict[str, dict[str, Any]] = {}

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

        def index(self, refs: list[str] | None = None) -> IndexResult:
            count = len(refs) if refs is not None else len(self._notes)
            return IndexResult(indexed_count=count, deleted_count=0, errors=[])

        def reindex(self) -> IndexResult:
            return self.index()

        def index_status(self) -> dict[str, Any]:
            return {"status": "ready", "document_count": len(self._notes)}

    inst = CustomMemoryConnector()
    inst.write_note("welcome.md", "hello world", mode="create")
    check_memory_connector(inst)
    check_indexable_memory(inst)
