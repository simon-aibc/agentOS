# ADR 0003: Identity, Audit Provenance, and Migration Integrity

## Context

Multi-user workflows, autonomous runs, and enterprise deployments require immutable audit trails and safe workspace isolation. In early versions, principal identity could be influenced by untrusted HTTP client headers (`X-Actor-Label`), database migrations lacked atomic WAL backup safeguards, and runs recorded un-canonicalized workspace directory paths.

## Decision

We establish strict foundations for audit provenance, identity resolution, and database evolution:

1. **Immutable Principal Model**:
   - `Principal` identity (`id`, `kind`, `display`, `on_behalf_of`) is resolved strictly from server-trusted authentication context.
   - `LocalPrincipalResolver` ignores arbitrary client-supplied spoofing headers and derives identities deterministically (`token:execution`, `local:<user>`).

2. **Canonical Workspace Scoping**:
   - Every run and audit observation references a canonical `workspace_id` (derived from canonical workspace root), preserving `runs.workspace` for backward compatibility without relying on mutable directory strings.

3. **Additive-Only Migration Engine**:
   - All schema upgrades in SQLite are strictly managed via `agent_os/migrations.py`.
   - Ad-hoc DDL in runtime stores is forbidden. Destructive statements (`DROP`, `ALTER RENAME`) are rejected.
   - Migrations automatically perform WAL-aware checkpoints and atomic backups prior to applying DDL.

4. **Decoupled Permission & Audit Storage Seams**:
   - Permissions and observations use isolated, versioned SQLite stores (`permissions.db`, `observations.db`) with fail-closed query semantics and durable usage tracking.

## Consequences

- Actor identities recorded in run ledgers and event streams cannot be spoofed.
- Database upgrades are safely reversible with automated schema verification.
- Runtime storage evolution is backwards compatible with v2 workspaces.
