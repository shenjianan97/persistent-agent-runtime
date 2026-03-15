# Local Development

## Quick Start

```bash
# 1. Bootstrap the database (first time only)
KEEP_DB_CONTAINER=1 ./infrastructure/database/verify_schema.sh

# 2. Configure API keys
cp .env.localdev.example .env.localdev
# At least one LLM key: ANTHROPIC_API_KEY, OPENAI_API_KEY
# Optional: TAVILY_API_KEY (web_search tool)

# 3. Install dependencies and start
make install
make dev
```

## What `make install` Does

- runs `npm install` in `services/console`
- creates `services/worker-service/.venv` when missing
- installs worker dependencies with `pip install -e '.[dev]'`

## What `make dev` Does

- loads local overrides from `.env.localdev`
- uses sensible local defaults for `DB_DSN` and `VITE_API_BASE_URL`
- forwards `APP_DEV_TASK_CONTROLS_ENABLED` to the API, worker, and console when set
- checks the existing `persistent-agent-runtime-postgres` container and starts it if needed
- expects dependencies to already be installed via `make install`
- starts the console, API service, and worker in a single terminal with prefixed logs
- stops all child processes cleanly when you press `Ctrl+C`

On startup, `make dev` runs `services/model-discovery/main.py` to auto-discover available LLM providers from configured API keys and populate the `provider_keys` and `models` tables in PostgreSQL. The API service validates task submissions against these tables, and the console model selector is populated from `GET /v1/models`. Set `ANTHROPIC_API_KEY` for Claude models, `OPENAI_API_KEY` for GPT models, or both.

## Dev-Only Task Controls

The runtime includes dev-only endpoints and tools for testing failure and recovery flows locally:

- `POST /v1/dev/tasks/{taskId}/expire-lease` — forces a running task into lease-expiry recovery (simulates a worker crash)
- `POST /v1/dev/tasks/{taskId}/force-dead-letter` — forces a task into the dead-letter path
- `dev_sleep` tool — an agent tool that sleeps for a configurable duration, useful for exercising timeout and long-running-task behavior deterministically

These are disabled by default. To enable them:

```bash
APP_DEV_TASK_CONTROLS_ENABLED=true make dev
```

## Timing Configuration

For faster local recovery testing, you can shorten the lease, heartbeat, and reaper timings:

```bash
APP_DEV_TASK_CONTROLS_ENABLED=true \
LEASE_DURATION_SECONDS=10 \
HEARTBEAT_INTERVAL_SECONDS=2 \
REAPER_INTERVAL_SECONDS=5 \
REAPER_JITTER_SECONDS=0 \
make dev
```

These are also supported in `.env.localdev`:

- `LEASE_DURATION_SECONDS`: how long a worker lease lasts before another worker can reclaim it
- `HEARTBEAT_INTERVAL_SECONDS`: how often the worker refreshes its lease and registry heartbeat
- `REAPER_INTERVAL_SECONDS`: base interval for expired-lease and timeout scans
- `REAPER_JITTER_SECONDS`: random jitter added to the reaper interval

## Verifying Prerequisites

If you only want to verify runtime prerequisites without starting services, run:

```bash
make dev-check
```

`make dev-check` is non-mutating: it validates the environment and the database container state, but it does not start the database for you. `make dev` may start the existing database container if it is currently stopped.

## Database Bootstrap

This is a host-based development workflow, not a Docker Compose stack. The quick start's `verify_schema.sh` creates a named PostgreSQL container (`persistent-agent-runtime-postgres`) with the schema applied. On subsequent runs, `make dev` will start that container if it's stopped.

If you already have your own PostgreSQL instance, skip the script and apply the migrations manually:

```text
infrastructure/database/migrations/0001_phase1_durable_execution.sql
infrastructure/database/migrations/0002_worker_registry.sql
infrastructure/database/migrations/0003_dynamic_models.sql
```

To reset and re-verify the schema (destructive — drops all data):

```bash
make db-verify
```
