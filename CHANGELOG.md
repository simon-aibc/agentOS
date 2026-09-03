# Changelog

All notable changes are recorded here. The project follows semantic versioning
for public releases.

## [Unreleased]

### Added

- **Pluggable Store Discovery**: Added dynamic entry-point discovery for the `agent_os.stores` group via `resolve_store_backend()` and `list_store_backends()` in `agent_os.stores`, allowing third-party persistence adapters (e.g. Postgres) to be loaded as plugins.
- **Link-Ingest Skill**: Bundled a `link-ingest` skill for safe retrieval of a single URL.
- **Workspace-Aware Doctor**: `agent-os doctor` resolves backend bindings from the active workspace, and now fails instead of reporting OK when a workspace disables policy checks with `mode = "off"`.
- **Platform Limitation Documentation**: Added `docs/platform/known-limitations.md`, defining the supported deployment envelope and the claim boundary for the current release, alongside ADR 0004 recording the authority-before-delegation invariant.

### Changed

- **Public boundary cleanup:** renamed the internal graph state to `AgentState`, removed the bundled public-concierge surface, and added the optional opaque `task_signature` field to `POST /api/runs`. Internal `agent_os.*` imports remain unsupported; use only symbols exported from `agent_os.api`.
- **Dependency Update Policy**: Dependabot now opens pull requests for security advisories only, grouped into a single request per batch. Routine version bumps are disabled because every runtime dependency is pinned exactly in `pyproject.toml`.

### Fixed

- **Bounded Webhook Worker & Shutdown Drain**: `WebhookEventSink` now routes background delivery through a bounded FIFO queue and fixed worker pool, ensuring `emit()` never blocks the execution path, drops with a warning under queue saturation, and cleanly drains in-flight deliveries during graceful shutdown via `close()`.
- **Explicit Insecure HTTP Opt-in**: Webhook URLs now reject unencrypted `http://` schemes by default. Insecure HTTP requires explicit opt-in via workspace configuration `[webhooks] allow_insecure_http = true` or `allowed_internal_hosts`.
- **Workspace Composition Memory Routing**: Eliminated direct environment variable bypasses in `architect.py`, `brief_runtime.py`, and `cli/app.py`, ensuring all memory operations route through `composed_workspace().memory_connector` when a workspace is active.
- **Unresolvable Self-Update Path**: `agent-os update` advertised `pip install --upgrade agent-os-langgraph`, which cannot resolve because the package is not published to PyPI, and executed it directly under `--yes`. A source checkout now gets `git pull` guidance instead of an index install that would replace its editable install, and other installs target the git remote pinned to the resolved release tag.
- **Update-Check Resource Leak**: `update_check` now closes the `HTTPError` it catches, and the corresponding API test no longer reaches the network.
- **Warnings-As-Errors Gate On Fresh Installs**: Pinned `anyio` in the `dev` extra. anyio 4.15.0 deprecated the `anyio.abc.BlockingPortal` alias that Starlette's TestClient still dereferences at import time, which broke test collection under the documented `pytest -W error` gate on any fresh install.
- **Offline Test Isolation**: The suite now blocks real outbound network connections for every test, and the sandbox test actually installs its Codex CLI stub rather than silently skipping it.

## [2.4.0] — 2026-08-21

### Added

- **Plugin Runtime & Discovery**: `agent_os.plugins.PluginRegistry` discovering extensions across 7 entry-point groups (`agent_os.connectors`, `agent_os.memory_connectors`, `agent_os.backends`, `agent_os.policies`, `agent_os.skill_packages`, `agent_os.context_providers`, `agent_os.event_sinks`) with fail-closed loading and protected built-in names.
- **Identity & Audit Provenance**: Stable `Principal` model and `LocalPrincipalResolver` resolving server-trusted actor provenance (`id`, `kind`, `display`, `on_behalf_of`) preventing HTTP header spoofing.
- **Retrieval Lifecycle & Pre-Planner Context**: `IndexableMemory` protocol with bounded cold-start indexing, and non-blocking `ContextProvider` execution before planner nodes.
- **Event Egress & Anti-SSRF Webhook**: `@runtime_checkable` `EventSink` protocol and reference `WebhookEventSink` featuring HMAC-SHA256 signatures, DNS-rebinding-safe IP pinning, and strict payload privacy invariants.
- **Extension Conformance Kit**: Dependency-light testing package under `agent_os.testing` with reusable pytest mixins and assertion helpers for third-party satellite development.

## [2.3.0] — 2026-08-20

### Added

- **Self-update lifecycle (client-ready)**: `agent-os update` CLI detects the runtime (Docker vs pip/wheel) and prints/runs the correct upgrade path, backing up databases first; `--check` dry-runs, `-y/--yes`, `--pull`, and `--reload` flags supported.
- **Update discovery**: `agent_os/update_check.py` checks GitHub Releases with a 24h on-disk cache, a 3s timeout, and fail-closed behaviour. `/api/health` is cache-only (no inline network) and exposes `current_version`, `latest_version`, and `update_available`; the cache refreshes out-of-band from the FastAPI lifespan. New `POST /api/update/check` forces a refresh.
- **Additive migration runner**: `agent_os/migrations.py` applies additive-only DDL (`ADD COLUMN`, `CREATE TABLE/INDEX IF NOT EXISTS`), guards against destructive statements, tracks `PRAGMA user_version`, and takes a WAL-aware backup (`wal_checkpoint(TRUNCATE)` with sidecar fallback) before running. Wired into the runs, schedules, permission, and observation store initialisers; fail-closed rollback on error.
- **Semantic M/N post-execution evaluator**: `agent_os/semantic_judge.py` adds an evidence-bounded LLM judge for natural-language acceptance criteria, evaluated after execution alongside the existing deterministic checks. Token-capped and wrapped in a bounded timeout; no LangGraph node and no execution-state schema change.

### Fixed

- Semantic judge timeout is now truly bounded — the worker executor is shut down with `wait=False` so a hung LLM call cannot block the judge's return.
- Migration-runner WAL backup no longer risks losing committed frames still in the write-ahead log.

## [2.2.1] — 2026-08-18

### Added

- **Audit trace integrity & evidence provenance**: `strategy_assignments` table adds `strategy_version`, `selection_reason`, `selector_version`, and sanitized `evidence_summary` JSON blob columns with automatic schema migration.
- **Audit read API & CLI**: added `GET /api/observations/assignments/{run_id}` endpoint and `agent-os observations assignment <run_id>` CLI subcommand for inspecting strategy assignment audit records with workspace isolation.

### Fixed

- Preserved original `selection_reason` (`explicit`, `evidence_backed`, `exploration`, `default`) on workflow replay and resume instead of hardcoding `default`.

## [2.2.0] — 2026-08-18

### Added

- **Bounded adaptive planning foundation**: terminal Runtime runs record
  workspace-isolated `unknown` observations in SQLite; explicitly labelled
  outcomes aggregate by fixed strategy and task kind. The architect can select
  only an allowlisted, versioned planning directive through deterministic
  override, evidence, or balanced exploration. This never grants permissions,
  executes tools, or changes execution safety constraints.

## [2.1.0] — 2026-08-18

### Added

- **User-taught memory-write permissions**: explicit one-time, session, always-approve, and always-deny choices; durable SQLite rules with usage counters; local CLI list/revoke commands; and token-gated Runtime API management endpoints.
- **Runtime policy binding**: CLI graph streams and server runs carry the composed workspace policy into nested memory writes, with session grants cleared on terminal completion.

### Security

- Learned/session grants are limited to exact `memory.write` connector + mode + ref scopes. Generic network, communication, filesystem, payment, and privileged actions cannot create a reusable grant.
- High-risk taxonomy denial occurs before rule lookup. Workspace rules are isolated in `<workspace>/permissions.db` unless an explicit `AGENT_OS_PERMISSIONS_DB` override is configured. Runtime API administration requires `AGENT_OS_PERMISSIONS_ADMIN_TOKEN`.
- Private Runtime execution endpoints are loopback-only by default; remote binds require `AGENT_OS_EXECUTION_TOKEN`, and browser WebSocket approvals require a configured exact origin.

### Fixed

- Connector write failures propagate to callers instead of being reported as an indistinguishable rejected write.
- Native memory-write failures are surfaced as failed CLI, Runtime API, and WebSocket outcomes rather than a successful completion; Gbrain's upsert-only write API now requires explicit `overwrite`.

## [2.0.0] — 2026-08-09

### Added

- **Stable extension API** (`agent_os.api`): a frozen, identity-preserving public facade (18 exports — `MemoryConnector`, `BackendAdapter`, `PolicyEngine`, skill-package types, etc.), `py.typed`, `docs/EXTENDING.md`, and golden-signature + behavioral conformance tests that fail if a public signature changes. This is the v2 compatibility surface for extension authors.
- **Self-host Compose stack** (`docker-compose.yml` + `docs/self-hosting.md`): one-command bring-up of the backend container plus the operator console, with the console image pinned by immutable multi-arch GHCR digest and its browser API base wired to the backend at runtime.

### Notes

- Console image is published multi-arch (`linux/amd64` + `linux/arm64`) to `ghcr.io/simon-aibc/agent-os-console` and pulls anonymously.

## [1.8.0] — 2026-08-09

### Added

- **Scheduler** (`agent_os/scheduler.py` + `agent-os schedule`): local cron/interval jobs (`run` | `brief`) persisted in a dedicated derived SQLite file (WAL + busy_timeout; never shares the async checkpointer's DB, avoiding the r1.7 deadlock class).
- **Self-host container**: multi-stage `Dockerfile` (digest-pinned `python:3.11-slim`); runtime stage carries only the wheel + deps, checkpoints in `/data`, sandbox/vault in `/workspace`, HEALTHCHECK on `/api/health`. Smoke-verified: container boots healthy and `/api/health` returns 200.

### Fixed

- `agent-os serve` now awaits a `uvicorn.Server` on the running event loop instead of calling `uvicorn.run()` (which crashed with "Cannot run the event loop while another loop is running" inside the container).

### Changed

- v1.8 self-hosting ships the backend container only; bundling the private `agent-os-console` image into Compose is deferred to r1.9a after that image is containerized and published to GHCR.

## [1.7.2] — 2026-08-08

### Added

- Real `/api/graph` returns G-Brain nodes/edges from the memory connector, bounded by `GRAPH_MAX_NODES=200` with `?limit=` support and an empty-state fallback when gbrain is unreachable.

## [1.7.1] — 2026-08-08

### Fixed

- **CORS on the Runtime API**: `agent-os serve` now sends `Access-Control-Allow-Origin` for the operator console origin (`http://127.0.0.1:4100` / `localhost:4100` by default; override with `AGENT_OS_CORS_ORIGINS`). Without this, a browser console could not call the API. Found by browser-verifying the console against a live server.

## [1.7.0] — 2026-08-08

### Added

- **Run Ledger** (`agent_os/runs.py`): a durable record of graph invocations — `runs` + `run_events` tables with `create_run`, `append_event` (atomic per-run `seq`), `set_status`, `get_run`, `list_runs`, and `list_events(after=)`. Lives in a dedicated SQLite file derived from the checkpoint path (`checkpoints.runs.db`; override with `AGENT_OS_RUNS_DB`).
- **EventStore + SSE**: `agent_os/server/run_executor.py` drives the graph via `astream_events`, translating node/token events into the ledger and resolving each run to `interrupted`/`completed`/`error`. `GET /api/runs/{id}/events` streams Server-Sent Events with replay-from-`?after=<seq>` and live-tail to terminal status.
- **Runtime API** (`agent-os serve`): `POST`/`GET /api/runs`, `GET /api/runs/{id}` (with the pending interrupt prompt), `POST /api/runs/{id}/approve` (resumes via `Command(resume=...)`; 409 if not interrupted), and `POST /api/runs/{id}/cancel` (409 if terminal) — the runtime seam for external orchestrators and operator consoles.

### Fixed

- Fresh runs now seed the full initial graph state (`task` + backend binding), fixing a `KeyError('task')` that mocked tests hid.
- The run ledger uses its own SQLite file to avoid a deadlock between the synchronous ledger writer and the async LangGraph checkpointer when sharing one file on the same event loop (surfaced as immediate `database is locked` even under WAL + `busy_timeout`).

### Validation

- 437 offline tests pass with warnings treated as errors (`python -m pytest -W error`); ruff clean; CI on Python 3.11 and 3.12.
- Real end-to-end verified on the live graph: `create` → real node events → interrupt at the approval gate → `approve`/resume drives the executor node, with every outcome recorded in the ledger.

## [1.6.0] — 2026-08-08

### Added

- **Morning Brief engine** (`agent_os/brief.py`) and an ordered **context spine** (`system.md` → `invariants.md` → `goals.md` → `hot.md` → `AI/Memory/*.md`); `agent-os brief` CLI writes `AI/Briefs/YYYY-MM-DD.md` through the write-path, auto-approved by policy.
- **Runtime API** (`agent-os serve`): a localhost-first FastAPI interface from the `[serve]` extra exposing sessions, a chat WebSocket, brief, graph, and health endpoints — the seam between the runtime and any external UI.
- **PolicyEngine**: `PolicyEngine` Protocol + `LocalPolicy` reference with a 7-level side-effect taxonomy (`none`/`read`/`write`/`network`/`communication`/`payment`/`privileged` → `allow`/`deny`/`require_approval`) and an `apply_policy` gate helper; the v1.4 memory-write gate is now a thin adapter over it, unchanged in behavior.
- **Workspace v1** (`workspace.toml`): a composition primitive binding backends, skills, connectors, memory, policy, context sources, and limits; `agent-os --workspace <path>` for `run`/`chat`/`serve`. `department`/`organization` are metadata only — the core stays domain-agnostic. Two seeded reference workspaces under `examples/`.

### Fixed

- Architect node no longer crashes when a backend binding is present but profile resolution fails (guard on the resolved profile); summarization degrades gracefully when its model is unavailable.

### Validation

- 422 offline tests pass with warnings treated as errors (`python -m pytest -W error`); ruff clean; CI on Python 3.11 and 3.12.
- Real end-to-end verified: brief generation against a live `claude-code` backend; `agent-os serve` endpoints via TestClient.

## [1.5.0] — 2026-08-07

### Added

- Multi-turn conversational loop in CLI via `agent-os chat` command with clean handling of `/exit`, EOF, and Ctrl+C.
- State schema support for multi-turn via `conversation_summary` string in `AgentState` and a generalized summarizer `agent_os/summarize.py` to condense old messages while retaining gist.
- Seamless summarization integration via the active `architect` backend (defaults to `cli/claude-code` when applicable) configured through a new `summary` profile block (`threshold_tokens` and `keep_recent_n`).
- Standardized, conflict-free prompt assembly `[system_task] + [hot_context] + [conversation_summary]` at the `architect` node boundary.
- Session indexing using local SQLite (`agent_os/sessions.py`) providing `agent-os sessions list|inspect|delete` commands and auto-titling for conversation resumption.
- Automated, auto-approved session log appending into the connected vault's `AI/Logs/` path retaining `agent-os` provenance metadata upon chat exit.
- `recall_session` skill providing "hôm qua nói gì về X" capability by bounding `MemoryConnector.search()` scope tightly to the generated `AI/Logs/` path.

## [1.4.0] — 2026-08-07

### Added

- Write-path for memory: `WritableMemory` Protocol (kept separate from read-only `MemoryConnector` so community read-only connectors are not forced to implement it) plus `MemoryWriteResult`.
- `MarkdownVaultConnector.write_note` with `create`/`append`/`overwrite` modes, path-traversal sandbox guard bound to the connector's own `root_path`, and YAML frontmatter round-trip.
- `GbrainConnector.write_note` mapping to gbrain `put_page`, with provenance frontmatter (`agent`/`created`/`via`/`source`) and `agentos/` slug isolation for agent-written notes.
- Approval gate for vault mutation: `MemoryWriteProposal` (added to the checkpoint allowlist) with `evaluate_write_policy` (auto-approve for `AI/Logs/` appends, gate everything else) and `gated_write` reusing the existing `interrupt()` human gate; rejection commits nothing.
- Bounded hot-context injection at the architect boundary via `load_hot_context` (`hot.md` + `AI/Memory/*.md`, `max_chars`/`max_age_days` bounds, no full-vault scan), configured through a profile `HotContextConfig`; `hot_context` state field carries static session-start context, kept distinct from the v1.5 rolling summary.

### Fixed

- Standardized `MemoryConnector` return schema (`ref`-keyed) across `MarkdownVaultConnector` and `GbrainConnector` so interface-bound skills behave identically on both.
- `GbrainConnector` now calls the real gbrain tools (`get_page`, `list_pages`, `query`) instead of non-existent `read_note`/`list_notes`; read-path verified against a live gbrain server rather than mocks.
- `GbrainConnector.read_note` reads frontmatter from gbrain's top-level `frontmatter`/`title` fields (compiled_truth is body-only), so provenance survives a write→read round-trip — a defect the mocks hid, caught by a real integration run on `main`.

### Validation

- 380 offline tests pass with warnings treated as errors (`python -m pytest -W error`); ruff clean.
- Real gbrain read and write→read round-trips verified against a live server (env-gated integration tests), including provenance frontmatter and ephemeral-slug cleanup.

## [1.3.0] — 2026-08-07

### Added

- Core generalization: generic `PlanArtifact`/`ExecutionResult` and `ActionProposal`; `CodingPlan`/`CodingResult` subclasses preserve the coding contract; `ArchitectBrief`/`ExecutorReport` retained as silent aliases (deprecation deferred).
- Connector framework: `Connector` and `MemoryConnector` Protocols with `ConnectorRegistry`; `FilesystemConnector`, `MarkdownVaultConnector` (portable, zero-dependency), and `GbrainConnector` (wrapping the gbrain MCP).
- Skill packages: `manifest.toml` loader with the `vault_qa` example binding the `MemoryConnector` interface, plus a "build your first non-coding skill" tutorial.

### Validation

- 356 offline tests pass with warnings treated as errors; ruff clean.
- Non-coding end-to-end verified through `vault_qa` over `MarkdownVaultConnector` (unmocked).

## [1.2.0] — 2026-08-05

### Added

- `BackendAdapter` Protocol and `BackendRegistry` with collision detection and role validation.
- Migrate `ClaudeCodeAdapter` and `CodexAdapter` off hardcoded factory branches onto the registry.
- Real authentication status checks for the Claude Code and Codex CLI adapters via dedicated read-only subprocess probes.
- `agent-os doctor` subcommand with human-readable table and JSON output covering registered adapters, resolved configuration, checkpoint reachability, warnings, and a health verdict.
- TOML profile loader at `$XDG_CONFIG_HOME/agent-os/profiles.toml` with single-parent one-level `extends` inheritance, secret-key refusal, and precedence resolution `--profile > AGENT_OS_PROFILE > file default > env`.
- `ROUTER_MODE=direct-escalation` to skip Tier-2 structured routing entirely for architect-first workflows; default remains `cascade`.
- Checkpoint `BackendBinding` persistence with resume-time effective-value conflict detection, legacy-checkpoint handling, and `--force-rebind` escape hatch that always warns.
- Antigravity CLI adapter registered as a not-yet-supported stub gated on documented noninteractive invocation and enforceable permission modes; surfaces in `agent-os doctor` under a candidate grouping.

### Validation

- 343 offline tests pass with warnings treated as errors.
- Comprehensive smoke used the Claude Code CLI adapter for both architect and executor roles; Codex adapter coverage is verified by the offline test suite through the shared registry code path.

## [1.1.2] — 2026-08-03

### Fixed

- Generate OpenAI strict-compatible Codex schemas recursively, including
  `additionalProperties: false` and complete `required` arrays.
- Classify common Claude and Codex authentication failures with actionable
  re-authentication guidance and redacted excerpts.
- Raise structured router acceptance from `0.70` to `0.80` and add semantic
  coding examples that escalate instead of invoking incomplete write tools.

### Validation

- 263 offline tests pass with warnings treated as errors.
- A real Codex executor smoke test edited and compiled a sandbox file and
  returned `ExecutorReport.success=True`.

## [1.1.1] — 2026-08-03

### Fixed

- Close child-process stdin with `DEVNULL` so noninteractive Claude Code calls
  do not wait for piped input.
- Isolate default-suite model configuration from a developer's local `.env`.

## [1.1.0] — 2026-08-03

### Added

- Subscription-backed Claude Code and Codex CLI delegators for architect and
  executor roles.
- Read-only architect permission modes and sandbox-scoped executor modes.
- Shared CLI runner with credential stripping, argument blocking, output
  parsing, timeout handling, and temporary-schema cleanup.

### Design

- Delegate to CLI agents as subprocesses instead of wrapping them as
  `BaseChatModel` instances.
- Retry the read-only architect on transient failures; never automatically
  retry a side-effectful CLI executor.

## [1.0.1] — 2026-08-02

### Fixed

- Resume checkpoints paused mid-run without requiring a pending HITL interrupt.
- Propagate nonzero Bash results to workflow and CLI exit status.
- Document multiline Tier-1 write command escaping.
- Retry transient API-model failures with bounded exponential backoff.

## [1.0.0] — 2026-08-02

### Added

- Typed state and compiled LangGraph workflow.
- Conditional supervisor, three-tier dispatcher, architect, human gate, and
  sandboxed executor.
- SQLite persistence with restart/resume coverage.
- Native tools, optional MCP loading, streaming CLI, and security boundaries.
- Prompt caching, message trimming, and retained-output caps.
- Python 3.11/3.12 CI, Ruff, warnings-as-errors tests, and MIT license.

[1.2.0]: https://github.com/simon-aibc/agent-os-langgraph/releases/tag/v1.2.0
[1.1.2]: https://github.com/simon-aibc/agent-os-langgraph/releases/tag/v1.1.2
[1.1.1]: https://github.com/simon-aibc/agent-os-langgraph/releases/tag/v1.1.1
[1.1.0]: https://github.com/simon-aibc/agent-os-langgraph/releases/tag/v1.1.0
[1.0.1]: https://github.com/simon-aibc/agent-os-langgraph/releases/tag/v1.0.1
[1.0.0]: https://github.com/simon-aibc/agent-os-langgraph/releases/tag/v1.0.0
