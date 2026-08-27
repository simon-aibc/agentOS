# Known platform limitations

- **Applies to:** Agent OS v2.4.x
- **Last verified:** 2026-08-27
- **Purpose:** define the supported deployment envelope and prevent ambiguous
  product claims

Agent OS is a local-first, self-hostable agent runtime. It provides resumable
graph execution, a persistent run ledger, human approval gates, policy
evaluation, and a stable extension surface. Those capabilities are deliberately
narrower than a distributed job platform, a multi-tenant authorization system,
or an enterprise workflow suite.

This document distinguishes shipped guarantees from nearby capabilities that
the current release does not provide. It is a statement of present behavior,
not a promise or roadmap.

## Supported deployment envelope

The supported v2.4 deployment shape is:

- one local or self-hosted Agent OS runtime;
- one composed `AGENT_OS_WORKSPACE` per server process;
- operator-controlled workloads and credentials;
- SQLite-backed checkpoints and local operational stores;
- explicit human approval for supported side-effect classes; and
- external container or microVM isolation for untrusted executable workloads.

Separate customer or trust domains should use separate deployments. Agent OS
does not currently claim native multi-tenant isolation, high availability,
automatic failover, or distributed execution semantics.

## Terms that must not be conflated

| Shipped term | What it guarantees | What it does not guarantee |
|---|---|---|
| **Durable checkpoint** | LangGraph state is persisted in SQLite and a known thread can resume from a saved checkpoint. | Automatic ownership, redelivery, retry, or recovery of background work after a worker or host failure. |
| **Run ledger** | Run metadata, status, events, approvals, and actor provenance are recorded. | A durable work broker, queue lease, exactly-once delivery, or dead-letter processing. |
| **Scheduler** | Cron and interval definitions persist, and a live runtime can claim due schedules and start bounded in-process tasks. | A distributed scheduler, heartbeat-based reclaim, or guaranteed execution while the service is offline. |
| **Principal provenance** | The runtime records a server-resolved actor identity for runs and approvals. | A complete identity provider, per-user session model, or role/attribute authorization system. |
| **Permission memory** | Exact native memory-write decisions can be remembered in a workspace-local permission store. | General tool authorization, per-user grants, RBAC, ABAC, or delegation authority. |
| **Artifact reference** | An `ExecutionResult` may return a list of string references to outputs. | Artifact storage, ownership, preview, versioning, retention, sharing, or access control. |
| **Workspace** | A declarative composition of backends, skills, connectors, memory, policy, context, limits, and event sinks. | A live worker identity, organization, department, tenant, or independently scheduled worker process. |

## 1. Checkpoint durability is not work-delivery durability

The checkpointer and the run ledger persist different kinds of state:

- the checkpointer persists graph state keyed by thread configuration;
- the run ledger persists run and event records; and
- the schedule store persists schedule definitions and their latest result.

Runtime API work is currently started with an in-process `asyncio` task.
Scheduled work is also dispatched into bounded in-process child tasks. The
`RunStore` contract records state transitions, but it does not define:

- a durable pending-work queue;
- worker ownership or a lease expiry;
- worker heartbeat;
- delivery or execution attempt count;
- automatic redelivery or reclaim after abrupt process loss;
- an idempotency or deduplication key;
- a dead-letter queue; or
- distributed concurrency control.

Consequently, a checkpoint may be resumable while the associated background
run is not automatically redispatched. An abrupt process or host failure can
leave a ledger row requiring operator inspection and explicit recovery. Agent
OS makes no exactly-once or at-least-once delivery guarantee for Runtime API or
scheduler dispatch.

The local scheduler advances a claimed occurrence before launching its
in-process child task. That prevents duplicate claims by local scheduler loops,
but it does not automatically recover the occurrence if the process stops after
the claim and before completion.

### Operational guidance

- Use graceful shutdown so active tasks can finish or be marked consistently.
- Back up checkpoint and operational SQLite files together.
- Inspect non-terminal run records after an unclean restart.
- Design external mutations to be idempotent where possible.
- Keep a manual reconciliation path for high-value side effects.
- Do not market the current runtime as a distributed durable queue or HA worker
  cluster.

## 2. Identity provenance is not authorization

`Principal` gives the ledger a trusted provenance seam: the server resolves the
actor rather than accepting an arbitrary actor label from a client. In the
local runtime, however, identity resolves to a local user, schedule identity, or
deployment-level execution-token identity. Agent OS does not ship an OIDC/SSO
login flow or a native multi-user authorization model.

Learned permission rules are narrower still:

- the supported remembered key is an exact
  `memory_write:write:<connector>:<mode>:<ref>` scope;
- the permission store looks up a rule by `permission_key`;
- `taught_by` and `workspace_id` are stored for provenance and inspection but
  are not query predicates; and
- practical workspace isolation relies on selecting a workspace-local store,
  not on row-level user authorization inside a shared permission database.

Therefore, two principals using the same permission store and exact key do not
receive distinct learned rules. `taught_by` answers "who taught this rule?"; it
does not answer "may this principal use this rule?"

### Deployment guidance

- Treat learned permissions as operator-taught workspace policy, not user
  entitlements.
- Use separate deployments or stores for separate trust domains.
- Put an external authenticated boundary in front of a private Runtime API.
- Do not claim native RBAC, ABAC, per-user permission memory, or row-level
  authorization.

## 3. High-tier actions are unavailable in the supported policy envelope

In the supported `manual` and `smart` modes, actions classified as `payment` or
`privileged` are denied. They are not converted into an approval request, and a
learned or session rule cannot approve them. When policy plugins are composed,
`CompositePolicyEngine` applies the high-tier denial before the base policy and
before every plugin, so an allow-all plugin cannot weaken it.

The current policy contract cannot express rules such as:

- "allow a payment below 1,000";
- "require CEO approval above 1,000";
- "permit this privileged action for an administrator role"; or
- "allow a delegated worker to spend within a budget."

`LocalPolicy(mode="off")` intentionally disables all checks, including these
denials. It exists as an unsafe local-only escape hatch for isolated testing and
is outside the supported production envelope. It must not be used as a way to
implement a business approval policy.

The policy engine trusts the `side_effect` classification supplied by the tool
or connector proposal. It does not independently discover that a mislabeled
custom operation is actually a payment or privileged action. Extension authors
must classify side effects correctly and test the negative policy path.

No additional payment-denial test work is implied by this limitation: default,
learning-policy, plugin-composition, and public-API fixture coverage already
exercise the classification and hard-floor behavior.

## 4. Workspace composition is not an organization or worker runtime

A workspace is currently loaded once and cached per server process. Department
and organization values are descriptive metadata; they do not create
department membership, worker lifecycle, authority, or routing behavior.

The shipped graph executes one bounded task state through planner, supervisor,
architect, human gate, executor, and tool-dispatcher nodes. It does not provide:

- a `WorkerInstance` lifecycle;
- a multi-worker registry inside one process;
- parent/child work items or a work DAG;
- delegation edges between principals or workspaces;
- authority budgets or delegation depth; or
- organization-wide inbox and assignment semantics.

Any future first-class delegation mechanism is constrained by
[`ADR 0004: Authority Before Delegation`](../adr/0004-authority-before-delegation.md).
That ADR establishes a safety invariant, not an implementation commitment.

## 5. Events are lifecycle egress, not an inbound event bus

`EventSink` and the built-in webhook sink emit run lifecycle events. The Runtime
API can be invoked over HTTP and schedules can start work, but the core does not
ship a durable inbound inbox that:

- consumes arbitrary third-party events;
- normalizes them into a canonical event schema;
- deduplicates delivery;
- materializes events into work items; or
- tracks acknowledgement and replay.

An extension may use the Runtime API as an integration boundary, but delivery
semantics remain the extension's responsibility.

## 6. Connector contracts do not provide business transaction semantics

The public `Connector` protocol defines capability description, side-effect
classification, and invocation. It intentionally does not define universal:

- business entities or an ontology;
- credential provisioning and rotation;
- request idempotency;
- retries and backoff;
- transactional outbox behavior;
- compensation or rollback; or
- source-system reconciliation.

The public repository ships the framework and reference integrations, not broad
coverage for finance, CRM, HR, LMS, or other business systems. Connector authors
and private deployments own the source-system contract and its failure model.

## 7. Artifact references are not an artifact subsystem

`ExecutionResult.artifacts` is a `list[str]`. Agent OS does not interpret those
strings or provide a built-in artifact repository. In particular, core does not
currently manage artifact binaries, MIME types, ownership, project membership,
versions, previews, download conversion, retention, or sharing permissions.

Extensions can return references to files or external systems. Consumers must
define how those references are stored, secured, resolved, and expired.

## 8. Application controls are not host isolation

Sandbox path resolution, subprocess argument arrays, output caps, redaction,
policy checks, and human gates reduce application-level risk. They do not
isolate hostile code from the host kernel, network, CPU, memory, or every host
filesystem resource.

Run untrusted workloads in a container, microVM, or equivalent isolation layer.
Treat checkpoints, ledgers, event payloads, sandboxes, and connector credentials
as sensitive deployment data.

## Claim boundary

Safe public descriptions include:

- "resumable, SQLite-checkpointed local execution";
- "persistent local run ledger and approval provenance";
- "workspace-scoped learned rules for exact native memory writes";
- "self-hostable single-runtime deployment"; and
- "extensible connector, policy, memory, context, backend, skill, and event-sink
  contracts."

Do not describe v2.4 as:

- a durable distributed job system;
- a multi-tenant enterprise control plane;
- a per-user RBAC/ABAC authorization service;
- an AI organization or autonomous worker fleet;
- a transactional integration platform; or
- an artifact management and collaboration suite.

## Verification anchors

The current boundaries are visible in:

- `agent_os/server/api.py` and `agent_os/scheduler.py` — in-process task
  dispatch;
- `agent_os/stores/protocols.py` — present `RunStore` and `ScheduleStore`
  contracts;
- `agent_os/policy.py` — high-tier policy behavior and exact learned-key
  derivation;
- `agent_os/permission_store.py` — permission lookup and provenance fields;
- `agent_os/server/runtime.py` — process-cached workspace composition;
- `agent_os/state.py` and `agent_os/graph.py` — single-task graph state and
  topology;
- `agent_os/events.py` and `agent_os/webhooks.py` — lifecycle event egress; and
- `agent_os/schemas.py` — artifact references on `ExecutionResult`.

Update this document whenever a shipped release changes one of these contracts.
