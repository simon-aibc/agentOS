# Self-hosting

Agent OS runs the runtime API and operator console locally with Docker Compose.
Both published ports bind to `127.0.0.1`; the stack is not designed for direct
internet exposure.

## Prerequisites

- Docker with Compose v2
- A checkout of this repository
- Provider credentials for any hosted models you select, or a reachable local
  model endpoint
- An anonymously pullable, digest-pinned `agent-os-console` image in
  `docker-compose.yml`

## Quickstart

```bash
cp .env.example .env
# Edit .env: choose the three LLM roles, add only the keys they need, and set
# AGENT_OS_WORKSPACE to the host directory the agents may access.
docker compose up --build --detach
docker compose ps
```

Open the console at <http://127.0.0.1:4100>. The runtime health endpoint is
<http://127.0.0.1:4680/api/health>.

The stack has two services:

- `backend` builds the local Agent OS wheel, runs `agent-os serve`, mounts the
  selected host workspace at `/workspace`, and stores runtime databases in the
  `agentos_data` volume at `/data`.
- `console` runs the separately published console image. Browser requests use
  `http://127.0.0.1:4680`, while Compose waits for the backend health check
  before starting it.

## Configuration

Compose reads `.env` from the repository root. Do not commit that file.

| Variable | Default | Purpose |
|---|---|---|
| `LLM_ROUTER` | `ollama/qwen2.5:14b` | Router model in LiteLLM provider/model form |
| `LLM_ARCHITECT` | `anthropic/claude-opus-4-8` | Architect model |
| `LLM_EXECUTOR` | `openai/gpt-5.5` | Executor model |
| `ANTHROPIC_API_KEY` | empty | Passed through only when set |
| `OPENAI_API_KEY` | empty | Passed through only when set |
| `AGENT_OS_WORKSPACE` | `.` | Host directory mounted read/write at `/workspace` |
| `AGENT_OS_API_BASE` | `http://127.0.0.1:4680` | Browser-visible runtime API used by the console |
| `AGENT_OS_CORS_ORIGINS` | `http://127.0.0.1:4100` | Allowed console origin |
| `AGENT_OS_SCHEDULER_ENABLED` | `true` | Enables automatic schedule firing |
| `AGENT_OS_SCHED_TICK_SECONDS` | `1` | Scheduler polling cadence |

Inside Compose, the runtime paths are deliberately fixed:

- checkpoints: `/data/checkpoints.db`
- run ledger: `/data/checkpoints.runs.db` (derived automatically)
- schedules: `/data/checkpoints.sched.db`
- sandbox/workspace: `/workspace`

The corresponding direct-host settings in `.env.example` do not override
these container paths.

Subscription CLI backends such as `cli/claude-code` and `cli/codex` require
their binaries and authentication inside the container; host installations and
login sessions are not inherited. Prefer API-backed models, use a reachable
local provider, or build a deliberate custom image rather than mounting broad
host credential directories.

## Operations

```bash
docker compose ps
docker compose logs --follow backend console
docker compose up --build --detach   # rebuild/update the backend and restart
docker compose down                  # stop; preserve the named data volume
```

Do not add `--volumes` to `docker compose down` unless you intend to delete all
runtime state.

## Backup and restore

The `agentos_data` volume contains checkpoints, the run ledger, and schedules.
Stop the stack before copying SQLite files so the backup is consistent.

```bash
docker compose down
docker run --rm \
  --volume agent-os_agentos_data:/data:ro \
  --volume "$PWD:/backup" \
  alpine tar czf /backup/agentos-backup.tar.gz -C /data .
```

The actual volume name includes the Compose project name; confirm it with
`docker volume ls` before backup or restore. Restore only into an empty or
intentionally replaceable volume, with both services stopped.

The backend image runs as the non-root `agentos` user. If a replacement volume
or bind mount produces permission errors, fix ownership on that specific mount;
do not run the service as root.

## Troubleshooting and security

```bash
docker compose config --quiet
docker compose ps
curl --fail http://127.0.0.1:4680/api/health
curl --fail http://127.0.0.1:4100/
```

- If the console image cannot be pulled anonymously by its exact digest, the
  release is not ready. A locally cached image is not valid release evidence.
- If port `4100` or `4680` is already in use, stop this stack and identify the
  owner. Do not kill or unload unrelated services. When changing ports, keep
  `AGENT_OS_API_BASE` and `AGENT_OS_CORS_ORIGINS` consistent.
- Keep provider keys only in the ignored `.env` file or your deployment secret
  system. Compose contains variable names, never credential values.
- Do not change the host bindings to `0.0.0.0` without an authenticated reverse
  proxy, TLS, and an explicit threat review. The runtime API has no public-edge
  authentication boundary.
