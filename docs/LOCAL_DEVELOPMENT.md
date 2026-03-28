# Local Development

## Quick Start

```bash
# 1. Bootstrap the database (first time only)
make init

# 2. Configure API keys
cp .env.localdev.example .env.localdev
# At least one LLM key: ANTHROPIC_API_KEY, OPENAI_API_KEY
# Optional: TAVILY_API_KEY (web_search tool)

# 3. Install dependencies and start
make install
make start
```

## What `make install` Does

- runs `npm install` in `services/console`
- creates `services/worker-service/.venv` when missing
- installs worker dependencies with `pip install -e '.[dev]'`

## What `make start` Does

- loads local overrides from `.env.localdev`
- uses sensible local defaults for `DB_DSN` and `VITE_API_BASE_URL`
- starts the local Langfuse Docker stack automatically
- forwards `APP_DEV_TASK_CONTROLS_ENABLED` to the API, worker, and console when set
- checks the existing `persistent-agent-runtime-postgres` container and starts it if needed
- expects dependencies to already be installed via `make install`
- runs model discovery before starting services
- starts the console, API service, and worker in the background
- waits for the console, API health endpoint, and requested worker count before reporting success
- writes PID and log files to `.tmp/`
- works with `make stop`, `make status`, and `make logs`

## Local Langfuse Observability

Langfuse is part of the default local development stack.

- `make start` always runs `make langfuse-up`.
- `make check` and local startup fail fast if `LANGFUSE_HOST`, `LANGFUSE_PUBLIC_KEY`, or `LANGFUSE_SECRET_KEY` are missing.
- Use `make langfuse-status` to inspect the local Langfuse containers and `make langfuse-down` to stop them.

The checked-in local stack initializes a local Langfuse organization, project, and API keys automatically with the credentials from `.env.localdev.example`:

- Langfuse UI: `http://127.0.0.1:3300`
- Default login: `local@example.com` / `LocalDevPass123!`
- Default project keys: `pk-lf-local` / `sk-lf-local`

On startup, `make start` runs `services/model-discovery/main.py` to auto-discover available LLM providers from configured API keys and populate the `provider_keys` and `models` tables in PostgreSQL. The API service validates task submissions against these tables, and the console model selector is populated from `GET /v1/models`. Set `ANTHROPIC_API_KEY` for Claude models, `OPENAI_API_KEY` for GPT models, or both.

## Dev-Only Task Controls

The runtime includes dev-only endpoints and tools for testing failure and recovery flows locally:

- `POST /v1/dev/tasks/{taskId}/expire-lease` — forces a running task into lease-expiry recovery (simulates a worker crash)
- `POST /v1/dev/tasks/{taskId}/force-dead-letter` — forces a task into the dead-letter path
- `dev_sleep` tool — an agent tool that sleeps for a configurable duration, useful for exercising timeout and long-running-task behavior deterministically

These are disabled by default. To enable them:

```bash
APP_DEV_TASK_CONTROLS_ENABLED=true make start
```

## Timing Configuration

For faster local recovery testing, you can shorten the lease, heartbeat, and reaper timings:

```bash
APP_DEV_TASK_CONTROLS_ENABLED=true \
LEASE_DURATION_SECONDS=10 \
HEARTBEAT_INTERVAL_SECONDS=2 \
REAPER_INTERVAL_SECONDS=5 \
REAPER_JITTER_SECONDS=0 \
make start
```

These are also supported in `.env.localdev`:

- `LEASE_DURATION_SECONDS`: how long a worker lease lasts before another worker can reclaim it
- `HEARTBEAT_INTERVAL_SECONDS`: how often the worker refreshes its lease and registry heartbeat
- `REAPER_INTERVAL_SECONDS`: base interval for expired-lease and timeout scans
- `REAPER_JITTER_SECONDS`: random jitter added to the reaper interval

## Running Multiple Workers

The system supports running multiple workers in the background for local multi-worker testing:

```bash
make start-worker N=3       # start 3 workers (default: 1)
make start N=3              # full stack with 3 workers
make scale-worker N=5       # scale up to 5 workers
make scale-worker N=2       # scale down to 2 workers
make stop-worker            # stop all workers
make status                 # see each worker's status
```

Each worker auto-generates a unique ID (`worker-{hostname}-{pid}-{uuid}`) and gets its own PID and log file in `.tmp/` (e.g., `worker-1.pid`, `worker-1.log`). Workers share the same database and compete for task leases via `FOR UPDATE SKIP LOCKED`.

## Verifying Prerequisites

If you only want to verify runtime prerequisites without starting services, run:

```bash
make check
```

`make check` is non-mutating: it validates the required tools, local dependencies, Python version, and API-key configuration. It does not start any services or the database container.

If you want to inspect what a target would do without running it, use `make -n <target>`. For example, `make -n start N=3` prints the commands for the three-worker startup flow without launching services.

## Database Bootstrap

This is a host-based development workflow, not a Docker Compose stack. The quick start's `make init` creates a named PostgreSQL container (`persistent-agent-runtime-postgres`) with the schema applied. On subsequent runs, `make start` will start that container if it's stopped.

If `DB_DSN` still points at `localhost` or `127.0.0.1`, the `db-up`, `db-migrate`, and `db-reset-verify` targets will derive the Docker-managed database name, user, password, and published port from that DSN. If `DB_DSN` points at a non-local host, those targets intentionally fail fast instead of trying to manage the wrong database; in that case, start your PostgreSQL instance manually and apply the migrations yourself.

If you already have your own PostgreSQL instance, skip the script and apply the migrations manually:

```text
infrastructure/database/migrations/0001_phase1_durable_execution.sql
infrastructure/database/migrations/0002_worker_registry.sql
infrastructure/database/migrations/0003_dynamic_models.sql
```

To reset and re-verify the schema (destructive — drops all data):

```bash
make db-reset-verify
```
