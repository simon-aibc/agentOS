# Public-boundary consumer audit

Audit date: 2026-08-24.

## Supported surface

The supported extension surface is `agent_os.api`. Imports from internal
`agent_os.*` modules are unsupported, as documented in `EXTENDING.md`.

The Runtime API baseline includes localhost health, update, sessions, brief,
graph, runs and run lifecycle/SSE, schedules, permissions, observations, skill
authoring, and WebSocket chat endpoints. `POST /api/runs` remains backward
compatible and now optionally accepts `task_signature`.

## Consumer map

Repository search of the private application found only Runtime API consumers:
`/api/runs`, run detail, approve, cancel, event streaming, and related read
endpoints. No source consumer called `/api/public/concierge/*`, imported
`agent_os.state`, imported public-concierge modules, or referenced
`simos_public_chat`.

## Disposition

The concierge routes, modules, tests, documentation, and bundled skill are
removed from Agent OS. A website-chat channel is application-owned code and may
be reintroduced only as an explicitly specified, vendor-neutral optional
plugin. The live application workflow is not modified by this cleanup.

## Compatibility

`AgentState` is the internal graph-state name. Checkpoint payload shape is
unchanged because the TypedDict key set and persisted models did not change.
The run-ledger schema receives only additive migration `0003`, which adds a
nullable `task_signature` column; existing rows and request bodies remain valid.
