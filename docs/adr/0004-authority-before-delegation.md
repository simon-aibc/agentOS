# ADR 0004: Authority Before Delegation

- **Status:** Accepted
- **Date:** 2026-08-27

## Context

Agent OS records server-resolved `Principal` provenance and applies policy at
side-effect boundaries, but it does not currently implement first-class
delegation between workers, principals, or workspaces.

A delegation edge is an authority transfer, not merely a routing decision. If a
runtime adds parent/child work or multi-worker dispatch before defining that
transfer, later authorization work must reinterpret historical runs, approvals,
connector calls, and credential use without knowing what authority was actually
granted. That missing history cannot be reconstructed reliably.

## Decision

**Agent OS must define and enforce an authority contract before introducing any
first-class delegation edge.**

The invariant is:

> Delegation may preserve or narrow verified authority; it must never create,
> infer, or expand authority from a prompt, role label, workspace name, or model
> decision.

Before delegated work begins, its durable record must identify at least:

- the verified delegating principal;
- the effective actor and any `on_behalf_of` chain;
- the target worker or execution boundary;
- the parent run and child run or work-item identifiers;
- the capabilities and resource scope being delegated;
- explicit constraints such as side-effect class, budget, expiry, or depth;
- the policy decision and approval provenance authorizing the delegation; and
- a stable contract version sufficient to interpret the record later.

The following rules apply:

1. Missing or unverifiable authority fails closed.
2. A delegate cannot grant authority it did not receive.
3. Child authority is no broader than parent authority.
4. Approval for one action or scope is not transferable unless its contract
   explicitly says so.
5. Policy is evaluated both when delegation is created and when a delegated
   side effect is executed.
6. Delegation provenance is appended before execution and remains auditable
   after completion, failure, cancellation, or replay.

## Consequences

- A future delegation feature cannot be implemented as supervisor routing alone.
- `Principal` and the run ledger provide useful seams, but do not by themselves
  constitute delegation authority.
- `Workspace` remains deployment composition, not a `WorkerInstance`, until a
  separate lifecycle and authority-bearing identity are explicitly introduced.
- Implementing delegation may wait for real usage; this ADR creates no roadmap
  commitment.
- Any proposal for worker fleets, parent/child runs, or multi-workspace dispatch
  must demonstrate conformance with this invariant before implementation.

## Non-decisions

This ADR does not select an RBAC or ABAC model, define an organization ontology,
mandate multi-tenancy, choose a durable queue, or authorize payment and
privileged actions. Those remain separate product and architecture decisions.

## Related records

- [`ADR 0003: Identity, Audit Provenance, and Migration Integrity`](0003-identity-and-audit.md)
- [`Known platform limitations`](../platform/known-limitations.md)
