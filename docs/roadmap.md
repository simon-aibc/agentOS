# Roadmap

Vision: **an open-source, local-first backbone for building durable,
controllable AI agent systems - generic primitives an engineering team can
clone, audit, extend, and self-host, without private context living in the
public repository.**

## Current State - v2.4.0

The first public backbone arc is complete:

- **v1.0-v1.2:** typed LangGraph runtime, deterministic tools, cascading
  router, human gate, durable SQLite checkpoints, MCP adapters, and backend
  profiles.
- **v1.3-v1.5:** generic contracts, connector framework, skill packages,
  memory read/write path, chat loop, session management, summarization, and
  cross-session recall.
- **v1.6-v1.8:** PolicyEngine, workspace composition, context spine, Runtime
  API, run ledger, SSE events, graph API, scheduler, backend container, and
  self-hosting foundation.
- **Console v1.9.2:** separately owned public operator console image published
  to GHCR as a digest-pinned multi-arch image.
- **v2.0.0:** stable `agent_os.api` extension surface, `py.typed`,
  compatibility tests, self-host Compose stack, and public extension docs.
- **v2.1.0:** workspace-scoped, user-taught permission memory for native
  memory writes, with explicit approval outcomes and fail-closed safety rules.
- **v2.2.0:** structured outcome observations and deterministic bounded
  selection among fixed, versioned planning strategies.
- **v2.2.1:** audit-trace integrity that preserves the original strategy
  selection reason and sanitized decision snapshot across replay.
- **v2.3.0:** self-update lifecycle, update discovery, additive migrations,
  and semantic M/N post-execution judge.
- **v2.4.0:** generic plugin runtime across 7 entry-point groups, identity audit
  provenance, retrieval lifecycle indexing, non-blocking pre-planner context,
  anti-SSRF signed webhook egress, and third-party conformance kit.

See [`CHANGELOG.md`](../CHANGELOG.md) for shipped release details.

## Public/Private Boundary

The public repository contains the framework, generic documentation, example
configuration, and tests. Private deployments supply credentials, checkpoints,
sandbox content, personal or client skills, memory, dashboards, bots, and
organization-specific policies through ignored files or external paths.

See [ADR 0001: Public vs Private](adr/0001-public-vs-private.md) for boundary
decisions.

## North Stars

1. **Backbone quality:** keep the runtime durable, controllable, observable,
   and easy to reason about.
2. **Dogfood validation:** validate the extension surface through real private
   deployments without leaking private context into the public core.
3. **External legibility:** make the project easy for community users to clone,
   self-host, audit, and extend.

## Completed Milestones

| Milestone | Status | Capability |
|---|---|---|
| v1.0 | Complete | Typed graph, supervisor routing, architect, human gate, executor, checkpoints |
| v1.1 | Complete | Claude Code and Codex subscription CLI delegators |
| v1.2 | Complete | Backend adapter registry, profiles, `agent-os doctor`, Antigravity candidate stub |
| v1.3 | Complete | Generic contracts, connector framework, skill packages |
| v1.4 | Complete | Vault memory write-path, approval policy, bounded hot context |
| v1.5 | Complete | Multi-turn chat, sessions, summarization, cross-session recall |
| v1.6 | Complete | PolicyEngine, workspace composition, context spine, Runtime API foundation |
| v1.7 | Complete | Run ledger, EventStore/SSE, approve/cancel Runtime API, graph API |
| v1.8 | Complete | Scheduler, backend self-host container |
| Console v1.9.2 | Complete | Public multi-arch GHCR operator console image |
| v2.0 | Complete | Stable extension API and one-command self-host Compose |
| v2.1 | Complete | Scoped, user-taught memory permissions with workspace isolation |
| v2.2 | Complete | Structured outcome evidence and bounded adaptive planning |
| v2.2.1 | Complete | Strategy assignment audit-trace integrity across replay |
| v2.3 | Complete | Self-update lifecycle, update discovery, additive migrations, semantic M/N judge |
| v2.4 | Complete | Plugin runtime, stable API facade, retrieval lifecycle, event egress & conformance kit |

## v2.x Direction

v2.x should stay conservative: improve adoption, extension ergonomics, and
deployment confidence before expanding the core.

### v2.3 - Deployment Hardening and Adoption

- Keep README, PRD, architecture, self-hosting, and extension docs current.
- Add/expand example workspaces for common deployment shapes.
- Add a "bring your own memory connector" tutorial using synthetic data.
- Document how to replace the default console with a private dashboard that
  calls the Runtime API.

- Add optional reverse-proxy guidance for authenticated private deployments.
- Improve backup/restore examples and runtime state inspection.
- Add more operational smoke tests for Compose, schedules, and event replay.

The v2.2 planning loop remains deliberately bounded. A future behavioral
learning system is not on the public roadmap until real usage produces enough
labelled outcome data to justify a new, explicitly reviewed contract.

### Future - Integration Adapters

- Evaluate backend adapters only when their noninteractive contracts and
  permission modes are enforceable.
- Keep Hermes, Telegram, personal dashboards, and client integrations in
  private deployments unless a generic adapter emerges.
- Promote only generic, tested contracts back into the public core.

## Repository Hardening

Maintained through GitHub settings and release discipline:

- protected `main` branch with required CI checks;
- warnings-as-errors offline tests and Ruff before release;
- automated dependency security updates where appropriate;
- no credentials, checkpoints, sandboxes, or private memory in Git;
- release notes attached to version tags.
