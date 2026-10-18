# Standalone Core installation

This guide targets the AgentOS Runtime source candidate. It does not require
Console, Hudos, a personal vault, or business connectors. Source installation is
the selected preparation path; the historical Docker/Console Compose files are
reference material and have not been requalified by this preparation.

## Install an identified source revision

Use the candidate's source manifest and revision rather than an unversioned
copy of a developer's working directory. From an extracted source directory:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install '.[serve]'
python -m pip check
```

The CI matrix declares Python 3.11 and 3.12. Local checks on another Python
version do not replace that matrix. Installation downloads dependencies unless
you provide a previously prepared wheelhouse. The package is not assumed to be
available on PyPI. Bundled examples and the top-level `skills/` directory belong
to the source distribution; do not assume a wheel contains those directories.

## First run in an empty workspace

Keep the environment activated, then change out of the source tree. These
commands use a new disposable directory, not an existing deployment:

```bash
probe_root="$(mktemp -d "${TMPDIR:-/tmp}/agentos-example.XXXXXX")"
mkdir -p "$probe_root/workspace" "$probe_root/state"
printf '%s\n' 'Hello from an isolated AgentOS workspace.' > "$probe_root/workspace/sample.txt"
export AGENT_OS_WORKSPACE=""  # Native mode; no workspace.toml is required
export AGENT_OS_SANDBOX="$probe_root/workspace"
export AGENT_OS_CHECKPOINTS_DB="$probe_root/state/checkpoints.db"
export AGENT_OS_SCHED_DB="$probe_root/state/schedules.db"
export AGENT_OS_SCHEDULER_ENABLED=false
export MCP_FILESYSTEM_ENABLED=false
export MCP_CODEGRAPH_ENABLED=false
export MCP_GBRAIN_URL=""
cd "$probe_root/workspace"
agent-os 'read_file sample.txt' --thread-id first-read --sandbox "$probe_root/workspace"
```

Expected behavior: the explicit native read command returns `sample.txt` through
the deterministic dispatch path. This example does not need provider keys.
The command's success alone does not measure total provider calls, external
traffic, or cost across the application. Local state remains under the
new directory for inspection; remove only that directory when finished.

## Optional local API

In the same isolated environment, after confirming the chosen port is unused:

```bash
agent-os serve --host 127.0.0.1 --port 14680
```

Health is available at `http://127.0.0.1:14680/api/health`. No Console is needed.
The server can perform update checks and configured integration work; it is not
an offline diagnostic merely because it binds to loopback.

For a shared machine, set a fresh `AGENT_OS_EXECUTION_TOKEN` in the server's
secret environment before starting. Private API clients send it as
`Authorization: Bearer <token>` or `X-Execution-Token`. Never embed it in a
browser bundle. Non-loopback binding requires the execution token, and remote
access also requires a deliberately configured authenticated edge and TLS.

## Operations and limits

- Keep checkpoint, run-ledger, schedule and other selected stores under an
  explicitly owned data root. Protect them as sensitive data.
- For a simple consistent backup, stop only this owned runtime and copy its
  whole state directory, including any SQLite sidecars; validate restoration in
  a disposable deployment. Copying an active database file alone is insufficient.
- A saved checkpoint supports explicit resume; it does not provide a durable
  worker queue or automatic recovery after a crash.
- Enable schedules only after reviewing their task, workspace, timing and
  integration permissions. Disable with `AGENT_OS_SCHEDULER_ENABLED=false`.
- Plugins execute trusted code. Native path checks do not contain hostile
  subprocesses or their network access.

See [release scope and configuration](public-release.md),
[limitations](platform/known-limitations.md), and [security](../SECURITY.md).
