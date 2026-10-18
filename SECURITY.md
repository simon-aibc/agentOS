# Security policy

## Supported versions

The current candidate is `2.5.0rc1`. Simon Tran is the maintainer and reporting
contact owner. Support is best effort, with no guaranteed response time or
long-term-support window. Report the exact version/commit; fixes are evaluated
for the current development line. See [support scope](SUPPORT.md).

## Reporting a vulnerability

Do not open a public issue for credentials, sandbox escapes, unsafe subprocess
behavior, checkpoint deserialization problems, or other exploitable findings.
If GitHub's **Report a vulnerability** flow is enabled in the repository
Security tab, use it to keep reports private. The maintainer is responsible for monitoring that channel.
If it is unavailable, request a private reporting channel without posting
sensitive reproduction details in a public issue.

Include:

- the affected version or commit;
- the smallest safe reproduction;
- expected and observed behavior;
- whether files, credentials, subprocesses, or checkpoints are exposed;
- any known workaround.

## Security boundary

Agent OS applies path checks, fixed subprocess working directories, permission
modes, credential redaction, and human approval to reduce accidental damage.
These controls are not OS-level isolation. They do not contain hostile code,
network access, child processes, CPU, or memory. Run untrusted workloads in a
container or microVM and keep checkpoint databases protected as sensitive data.

The private Runtime API uses a deployment execution token when configured;
without one, the supported local mode trusts loopback callers. CORS is a browser
origin control, not caller authentication. Place a separately authenticated
boundary in front of remote deployments. See [Core authority and configuration](docs/public-release.md).

## Verified reporting destination

The canonical repository is [simon-aibc/agentOS](https://github.com/simon-aibc/agentOS).
Private vulnerability reporting was observed enabled through the GitHub API on
2026-10-18. Use the repository Security tab for sensitive reports. Simon Tran is the owner-designated contact. No response-time SLA is promised.
