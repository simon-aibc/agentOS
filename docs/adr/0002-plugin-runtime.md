# ADR 0002: Plugin Runtime & Extension Architecture

## Context

As Agent OS evolved, integrations expanded across multiple axes: memory backends, action connectors, LLM providers, policy engines, context providers, and event sinks.

Coupling these third-party systems directly to the core repository introduces severe dependency bloat, fragile transitive requirements, and licensing conflicts. Furthermore, arbitrary runtime discovery mechanisms can compromise security if external plugins shadow critical built-in components or silently fail.

## Decision

We establish a unified, secure, fail-closed plugin runtime managed by `PluginRegistry`:

1. **Standard Entry Points**: All extension axes use Python standard `importlib.metadata.entry_points`:
   - `agent_os.connectors`
   - `agent_os.memory_connectors`
   - `agent_os.backends`
   - `agent_os.policies`
   - `agent_os.skill_packages`
   - `agent_os.context_providers`
   - `agent_os.event_sinks`

2. **Built-in Name Collision Protection**:
   Third-party plugins must NEVER override or shadow protected built-in names (e.g. `markdown`, `markdown_vault`, `gbrain`, `codex`, `claude`, `webhook`). Discovery fails closed unconditionally if a collision occurs.

3. **Fail-Closed Loading**:
   If a configured plugin fails to import, resolve, or satisfy its runtime protocol, Agent OS halts with an explicit error rather than silently falling back to a default implementation.

4. **Satellites & Heavy Integrations Remain Outside Core**:
   Ecosystem integrations (such as n8n webhooks, vector databases, rerankers, and specialized enterprise connectors) live in standalone satellite repositories that depend on the `agent_os.api` facade and validate compliance using `agent_os.testing`.

## Consequences

- The core framework remains lightweight and free from heavy vector/PDF/browser dependencies.
- Third-party packages have a stable, versioned contract (`agent_os.api`).
- Operational safety is guaranteed through fail-closed discovery and built-in name protection.
