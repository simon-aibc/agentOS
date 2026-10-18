# Support

AgentOS Runtime is a Core execution framework for trusted-operator deployments.
The current release candidate is 2.5.0rc1. It is intended for evaluation; this
policy does not claim enterprise production readiness or a long-term-support window.

- Maintainer and security contact owner: **Simon Tran**.
- Community support: **best effort**, with no guaranteed response time or 24/7 coverage.
- Bugs and documentation: use [GitHub issues](https://github.com/simon-aibc/agentOS/issues), including version, environment and a minimal synthetic reproduction.
- Sensitive findings: use [private vulnerability reporting](https://github.com/simon-aibc/agentOS/security/advisories/new). Do not post credentials, customer data or exploit details in public issues.
- Contributions: see [CONTRIBUTING.md](CONTRIBUTING.md).

## Supported scope

Core CLI/API, public extension contracts, local checkpoint/run state, and the
documented policy envelope. Python 3.11/3.12 are the declared CI matrix. Separate
trust domains need deliberately isolated deployments and application-owned
authorization. Third-party plugins execute trusted code; native tools are not
an OS sandbox. See [limitations](docs/platform/known-limitations.md).

Console, private Hudos applications, personal vaults, customer integrations and
provider accounts are not supplied or supported by this Core release policy.
Paid implementation and maintenance for a Hudos deployment require a separate
agreement defining scope, acceptance, support hours and responsibilities.
