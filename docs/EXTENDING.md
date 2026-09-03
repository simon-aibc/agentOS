# Extending Agent OS

Agent OS v2 guarantees stability only for symbols exported from `agent_os.api`.
Imports from internal `agent_os.*` modules are private implementation details and may change across minor releases.

---

## 1. Extension Axes & Protocols

Agent OS supports pluggable extension across multiple independent axes:

### Memory Connectors (`MemoryConnector`, `WritableMemory`, `IndexableMemory`)
- **`MemoryConnector`**: Synchronous read & search interface.
- **`WritableMemory`**: Write interface (`write_note`, `describe_write_side_effect`, `supported_write_modes`).
- **`IndexableMemory`**: Lifecycle indexing interface (`index`, `reindex`, `index_status`).

```python
from typing import Any
from agent_os.api import MemoryConnector, MemoryHit


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
```

```python
from typing import Any, Literal
from agent_os.api import MemoryWriteResult, WritableMemory


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
        # Perform write logic...
        return MemoryWriteResult(
            ref=ref,
            mode=mode,
            bytes_written=len(content.encode("utf-8")),
            committed=True,
        )
```

### Action Connectors (`Connector`)
Custom external tools and system integrations. Connectors declare their capabilities mapping and describe the side-effect category for each action:

```python
from typing import Any
from agent_os.api import Connector, ExecutionResult


class CustomActionConnector:
    name = "ops_tools"

    def capabilities(self) -> dict[str, Any]:
        return {"actions": ["fetch_metrics", "deploy_service"]}

    def describe_side_effect(self, action: str) -> str:
        if action == "fetch_metrics":
            return "read"
        return "write"  # Must be one of the 7-value taxonomy

    def invoke(self, action: str, args: dict[str, Any]) -> ExecutionResult:
        return ExecutionResult(status="completed", outputs={"status": "ok"})
```

### Pre-Planner Context Providers (`ContextProvider`)
Synchronously fetch contextual knowledge (e.g. database schemas, company glossaries) before planner execution without blocking the event loop:

```python
from agent_os.api import ContextBlock, ContextProvider, Principal


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
```

### Backend Adapters (`BackendAdapter`)
Integrate local or remote model execution backends:

```python
from agent_os.api import AuthStatus, BackendAdapter, BackendRole, ExecutionResult


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
```

### Event Egress Sinks (`EventSink`)
Export real-time lifecycle event notifications (`run.created`, `run.interrupted`, `run.approved`, `run.completed`, `run.failed`, `run.cancelled`):

```python
from typing import Any
from agent_os.api import EventSink


class ConsoleEventSink:
    name = "console_audit"

    def emit(self, event: dict[str, Any]) -> None:
        print(f"Lifecycle event: {event['event']} on run {event['run_id']}")
```

---

## 2. Plugin Discovery & Entry Points

Third-party packages register extensions in `pyproject.toml` using standard Python entry points:

| Extension Group | Protocol | Description |
|---|---|---|
| `agent_os.connectors` | `Connector` | Custom domain action tools and integrations |
| `agent_os.memory_connectors` | `MemoryConnector` | Custom memory and knowledge store connectors |
| `agent_os.backends` | `BackendAdapter` | Custom architect and executor model backends |
| `agent_os.policies` | `PolicyEngine` | Custom policy evaluation and governance engines |
| `agent_os.skill_packages` | `SkillPackageLoader` | Standalone packaged skills and tools |
| `agent_os.context_providers` | `ContextProvider` | Pre-planner context injection providers |
| `agent_os.event_sinks` | `EventSink` | Lifecycle event egress sinks (e.g. webhooks, audit) |

Example `pyproject.toml`:
```toml
[project.entry-points."agent_os.context_providers"]
db_schema = "my_ext.context:SchemaContextProvider"

[project.entry-points."agent_os.event_sinks"]
datadog = "my_ext.events:DatadogEventSink"
```

### Discovery & Collision Policy
- **Fail-Closed Loading**: If a configured plugin fails to import or satisfy its protocol, Agent OS raises an explicit error and halts.
- **Built-in Name Protection**: Third-party plugins cannot override protected built-in names (`markdown`, `markdown_vault`, `gbrain`, `codex`, `claude`, `webhook`, `console`). Collisions trigger an immediate fail-closed error.

---

## 3. Policies & Safety Boundaries

Policies synchronously decide whether a proposed action is allowed, denied, or requires human approval. `apply_policy` executes only an allowed proposal; approval uses the runtime's human-interrupt path.

```python
from typing import Any
from agent_os.api import ActionProposal, PolicyDecision, PolicyEngine, apply_policy


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
```

### Policy Modes
- **`manual` (default)**: Evaluates safely scoped learned memory rules, active session grants, built-in log/brief rules, and the 7-value taxonomy (`read`/`none` → `allow`, `write`/`network`/`communication` → `require_approval`, `payment`/`privileged` → `deny`). Requires interactive human approval for unknown low/medium actions.
- **`smart`**: Operates as a tested alias of `manual` with identical safety boundaries.
- **`off`**: Explicit **unsafe local-only escape hatch** intended strictly for isolated sandbox testing.

### Policy Safety Boundary Invariants
- **Additive Restrictions Only**: Custom policy plugins may add restrictions or require human approval.
- **Hard Safety Floor**: High-risk taxonomy denials (`payment` and `privileged`) remain unconditionally denied. A custom policy plugin **cannot** override or bypass these built-in denials.

---

## 4. User-Taught Permission Learning

Agent OS uses explicit, user-taught permission learning rather than autonomous self-learning:
- **Approve once** (`approved`, `y`): Grants access only for the immediate action.
- **Session** (`session`): Grants access to the same safely scoped memory action for the current CLI invocation or server run. It is cleared when that session ends.
- **Always approve** (`always_approve`): Persists an allow rule to SQLite across restarts.
- **Always deny** (`always_deny`): Persists a deny rule to SQLite across restarts.
- **Reject** (`rejected`, `n`): Cancels the action execution.

Remembered rules deliberately apply **only** to `memory.write`. Each key includes the actual connector, write mode, and full note ref:
```text
memory_write:write:<connector>:<create|append|overwrite>:<ref>
```

For example, approval to create a note never authorizes overwriting that note, and a `markdown_vault` rule never authorizes a `gbrain` write. Generic file, network, and communication tools do not expose a canonical destination schema, so `session` and `always_*` are rejected for them; use one-time `approved` instead. `payment` and `privileged` are denied before any rule is read.

For a workspace, rules live in `<workspace>/permissions.db`. Set `AGENT_OS_PERMISSIONS_DB` to explicitly override that location.

Learned rules can be inspected and revoked via the CLI:
```bash
agent-os permissions list
agent-os permissions list --json
agent-os permissions revoke <permission-key>
agent-os permissions list --workspace path/to/workspace.toml
```
Or via the Runtime API:
- `GET /api/permissions`
- `DELETE /api/permissions/{permission_key}`

---

## 5. Structured Observation & Outcome Evidence

Terminal Runtime runs add one structured observation with `outcome_signal = unknown`. A completed run is **not** evidence of user acceptance. Operators may explicitly label the observation `accepted`, `rejected`, or `edited`:

```bash
agent-os observations list --workspace path/to/workspace.toml
agent-os observations record-outcome <observation-id> --signal edited \
  --evidence "Adjusted the artifact before use" --workspace path/to/workspace.toml
```

The same data is available through the private execution API:
- `GET /api/observations`
- `POST /api/observations/{observation_id}/outcome`

Stores live in `<workspace>/observations.db` (or standalone `./observations.db`), unless `AGENT_OS_OBSERVATIONS_DB` explicitly overrides the path. The records contain bounded operational metadata only; they never store task prompt text, model output, tool arguments, or memory contents. Strategy assignment records maintain full audit provenance across workflow replays.

### Stable task grouping

`POST /api/runs` accepts an optional `task_signature` in the form
`sha256:v1:<64 lowercase hex characters>`. It is an opaque client-produced
digest used only to group observations. When absent, Agent OS hashes the exact
submitted `task`; it does not parse, trim, or recognize application envelopes.
An application that adds volatile context should hash its stable task shape and
send that digest itself.

```json
{"task":"Prepare a weekly report", "task_signature":"sha256:v1:<digest>"}
```

## Build your application

Keep the application and Agent OS independently runnable: the application
calls the localhost Runtime API, while a private workspace supplies its own
configuration and extension packages. Do not import internal `agent_os.*`
modules; only `agent_os.api` is stable.

For a vendor-neutral reference, start with
[`examples/private-workspace`](../examples/private-workspace). Its workspace
loads a local private skill package and uses built-in memory. The adjacent
`extensions/pyproject.toml` shows how the same private application registers a
context provider and action connector through standard entry points. Install
that extension package into the environment that runs Agent OS, then set the
provider/connector names in the private workspace. Credentials, profiles, and
application routes remain outside this repository.

---

## 6. Skill Packages

A skill package is trusted local Python code with this layout:

```text
my_skill/
├── manifest.toml
└── handlers.py
```

```toml
[skill]
name = "my-skill-package"
version = "1.0.0"

[[skill.handlers]]
match = ["hello", "hi"]
entrypoint = "handlers:hello"
```

```python
# handlers.py
def hello(name: str = "world") -> str:
    return f"Hello, {name}"
```

Load it using `SkillPackageLoader` and `SkillRegistry`:

```python
from pathlib import Path
from agent_os.api import SkillPackageLoader, SkillRegistry

skills = SkillRegistry()
SkillPackageLoader(skills).load_package(Path("my_skill"))
result = skills.get("hello").invoke({"name": "Ada"})
```

`name` and `version` are required. There must be at least one handler. Every handler needs a non-empty `match` list and a package-relative `module:function` entrypoint. Handlers are either LangChain `BaseTool` instances or plain callables invoked with keyword arguments.

---

## 7. Webhook & Event Sink Safety Guarantees

The reference `WebhookEventSink` enforces strict security invariants:

1. **Payload Privacy**: Webhook events contain sanitized operational metadata only (`event`, `run_id`, `workspace_id`, `status`, `timestamp`, `actor`, `references`). Payloads **never** include prompt text, model outputs, tool arguments/results, approval reasons, or secrets.
2. **Encrypted Transport by Default**: Webhook URLs must use HTTPS by default. Unencrypted HTTP is disallowed unless explicitly opted into via `[webhooks] allow_insecure_http = true` in workspace configuration or for hosts listed in `allowed_internal_hosts`.
3. **HMAC-SHA256 Signatures**: Payloads are signed with a shared secret using `X-AgentOS-Timestamp` and `X-AgentOS-Signature: sha256=<hex>`.
4. **SSRF & DNS-Rebinding Protection**: Target hostnames are resolved immediately before connecting, every IP address is checked against private/loopback/internal ranges, and the connection is pinned directly to the validated IP. TLS SNI and server certificate validation use the original domain name. Redirects are not followed to prevent token/signature forwarding.
5. **Bounded Worker Queue & Graceful Shutdown Drain**: Deliveries are buffered in a bounded FIFO queue serviced by a dedicated background worker pool. `emit()` never blocks runtime execution (dropping under saturation with a warning), and `close()` cleanly drains in-flight items during process shutdown.

---

## 8. Extension Conformance Kit

Satellite packages can import the standalone conformance kit under `agent_os.testing` to validate compliance in local CI:

```python
import pytest
from agent_os.testing import (
    check_memory_connector,
    check_connector,
    check_context_provider,
    check_backend_adapter,
    check_event_sink,
)
from my_package import MyMemoryConnector, MyActionConnector


def test_conformance():
    check_memory_connector(MyMemoryConnector())
    check_connector(MyActionConnector())
```

---

## 9. Typing & Compatibility

The distribution includes `py.typed`. Type annotations are part of the v2 public contract; use a Python type checker against `agent_os.api`, but retain runtime error handling for third-party implementations. Async protocol variants, dependency installation, remote package discovery, graph internals, CLI/HTTP internals, and concrete built-in connectors are not stable extension APIs.
