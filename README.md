# Persistent Agent Runtime

Durable execution infrastructure for AI agents.

This project is building a cloud-native runtime for long-running agent tasks that need to survive worker crashes, resume from checkpoints, and expose operational control over retries, leases, dead letters, and cost tracking.

## Why This Exists

Most agent frameworks are good at defining workflows, but not at running them safely in production. In practice:

- LLM calls are non-deterministic, so deterministic replay models break down
- agent tasks may run for minutes or hours, so crashes cannot mean starting over
- tool calls and multi-step execution need durable state, retries, and observability
- platform operators need queueing, worker coordination, and failure handling, not just prompt orchestration

This repo combines:

- a Java API service for task submission and querying
- a Python worker service for lease-based execution and LangGraph orchestration
- a React console for monitoring tasks, workers, and dead letters
- PostgreSQL as the Phase 1 queue and durable checkpoint store

## Current Architecture

Phase 1 uses a database-as-queue model:

1. Clients submit a task through the API service.
2. The task is stored in PostgreSQL in `queued` state.
3. A worker claims the task with `FOR UPDATE SKIP LOCKED`.
4. The worker executes the LangGraph workflow and writes checkpoints to PostgreSQL.
5. Heartbeats extend the lease while work is in progress.
6. A reaper recovers expired leases and handles timeout/dead-letter transitions.

Core properties:

- checkpoint-resume instead of event-sourced replay
- lease-based task ownership
- dead-letter and redrive support
- per-step checkpoint history and cost tracking
- read-only Phase 1 tools exposed through a co-located MCP server

For local testing, the runtime also supports optional dev-only task controls:

- `/v1/dev/tasks/{taskId}/expire-lease` to force a running task into normal lease-expiry recovery
- `/v1/dev/tasks/{taskId}/force-dead-letter` to force a task into the normal dead-letter path
- a dev-only `dev_sleep` tool so long-running tasks and timeout behavior can be exercised deterministically

## Repository Layout

```text
docs/
  PROJECT.md
  design/
  implementation_plan/
services/
  api-service/
  console/
  worker-service/
tests/
  backend-integration/
experiments/
  langgraph/
infrastructure/
  database/
  cdk/
```

- [`docs/PROJECT.md`](./docs/PROJECT.md): project overview, phases, tradeoffs, and roadmap
- [`docs/design/`](./docs/design/): architecture and design documents
- [`docs/implementation_plan/`](./docs/implementation_plan/): implementation planning and progress
- [`services/api-service/`](./services/api-service/): Spring Boot API service
- [`services/console/`](./services/console/): React SPA for monitoring and controlling the runtime
- [`services/worker-service/`](./services/worker-service/): Python worker, checkpointer, executor, and tools
- [`tests/backend-integration/`](./tests/backend-integration/): cross-service integration tests (API + Worker + PostgreSQL, mocked LLMs)
- [`experiments/langgraph/`](./experiments/langgraph/): proof-of-concept and validation work
- [`infrastructure/database/`](./infrastructure/database/): schema migrations and verification
- [`infrastructure/cdk/`](./infrastructure/cdk/): AWS CDK infrastructure (Task 8)

## Getting Started

### Prerequisites

- Java 21+
- Python 3.11+
- Node.js 18+
- PostgreSQL
- Docker

### First-Run Database Setup

The API service and worker service expect the Phase 1 PostgreSQL schema to already exist.

Warning: `./infrastructure/database/verify_schema.sh` and `make db-verify` are destructive verification flows. They reset the `public` schema with `DROP SCHEMA ... CASCADE` before recreating the tables from the migration. Do not run them against a PostgreSQL database you want to preserve.

The easiest bootstrap path is the provided verification script, which uses Docker to start a disposable PostgreSQL instance, apply the schema, and verify it:

```bash
./infrastructure/database/verify_schema.sh
```

If you want to keep that PostgreSQL container running for local development:

```bash
KEEP_DB_CONTAINER=1 ./infrastructure/database/verify_schema.sh
```

That gives you a local database with the required `tasks`, `checkpoints`, and `checkpoint_writes` tables already created.

If you already have your own PostgreSQL instance, Docker is not strictly required. In that case, apply the migrations manually in order:

```text
infrastructure/database/migrations/0001_phase1_durable_execution.sql
infrastructure/database/migrations/0002_worker_registry.sql
```

Then point the API and worker services at that database with their normal environment variables.

### Useful Entry Points

- API service: [`services/api-service/README.md`](./services/api-service/README.md)
- Console: [`services/console/README.md`](./services/console/README.md)
- Worker service: [`services/worker-service/README.md`](./services/worker-service/README.md)
- Backend integration tests: [`tests/backend-integration/README.md`](./tests/backend-integration/README.md)
- Database schema: [`infrastructure/database/README.md`](./infrastructure/database/README.md)

### Common Commands

Use the root `Makefile` for the common workflows:

```bash
make install
make dev
make dev-check
make api-test
make worker-test
make e2e-test
make db-verify
make clean
```

### One-Command Local Development

For the default local development flow, use the root launcher instead of starting the console, API, and worker in separate terminals.

```bash
cp .env.localdev.example .env.localdev
# Fill in ANTHROPIC_API_KEY and TAVILY_API_KEY
make install
make dev
```

Recommended local workflow:

```text
fresh clone
-> copy .env.localdev
-> make install
-> make dev
```

What `make dev` does:

- loads local overrides from `.env.localdev`
- uses sensible local defaults for `DB_DSN` and `VITE_API_BASE_URL`
- forwards `APP_DEV_TASK_CONTROLS_ENABLED` to the API, worker, and console when set
- checks the existing `persistent-agent-runtime-postgres` container and starts it if needed
- expects dependencies to already be installed via `make install`
- starts the console, API service, and worker in a single terminal with prefixed logs
- stops all child processes cleanly when you press `Ctrl+C`

If you want the dev-only task controls and the `dev_sleep` tool available in the local console/API flow:

```bash
APP_DEV_TASK_CONTROLS_ENABLED=true make dev
```

What `make install` does:

- runs `npm install` in `services/console`
- creates `services/worker-service/.venv` when missing
- installs worker dependencies with `pip install -e '.[dev]'`

If you only want to verify runtime prerequisites without starting services, run:

```bash
make dev-check
```

`make dev-check` is non-mutating: it validates the environment and the database container state, but it does not start the database for you. `make dev` may start the existing database container if it is currently stopped.

This is a host-based development workflow, not a Docker Compose stack. It expects the named PostgreSQL container to already exist. If it does not, bootstrap it with:

```bash
KEEP_DB_CONTAINER=1 ./infrastructure/database/verify_schema.sh
```

For database bootstrap and verification:

```bash
make db-verify
```

Warning: `make db-verify` is destructive to existing data in the target database. It resets the `public` schema with `DROP SCHEMA ... CASCADE` before recreating the tables. Use it only against a disposable/local verification database.

## Development Status

The repo is in active development.

Implemented or substantially defined already:

- Phase 1 database schema and verification flow
- REST API for task submission, listing, status, checkpoints, cancellation, dead-letter listing, and redrive
- Worker poller, heartbeat manager, and reaper
- Worker registry with self-registration, heartbeat, and stale worker cleanup
- PostgreSQL-backed LangGraph checkpointer
- In-process MCP server for `web_search`, `read_url`, and `calculator`
- Dev-only task controls for forced lease expiry and dead-letter transitions
- Dev-only `dev_sleep` tool for deterministic timeout and long-running-task testing
- Console frontend: dashboard, task list, task dispatcher, execution telemetry, dead letter queue
- End-to-end test coverage for crash recovery and lifecycle behavior

Still evolving:

- AWS infrastructure and deployment flow (Task 8)
- Later-phase multi-agent scheduling and budget enforcement

## Design Documents

Start here if you want the actual system contract rather than the repo overview:

- [`docs/design/PHASE1_DURABLE_EXECUTION.md`](./docs/design/PHASE1_DURABLE_EXECUTION.md)
- [`docs/design/PHASE2_MULTI_AGENT.md`](./docs/design/PHASE2_MULTI_AGENT.md)
- [`docs/design/DESIGN_NOTES_PHASE3_PLUS.md`](./docs/design/DESIGN_NOTES_PHASE3_PLUS.md)

For implementation planning:

- [`docs/implementation_plan/phase-1/plan.md`](./docs/implementation_plan/phase-1/plan.md)
- [`docs/implementation_plan/phase-1/progress.md`](./docs/implementation_plan/phase-1/progress.md)

## Testing

There are three practical test layers in the repo:

- API service tests in `services/api-service`
- worker service tests in `services/worker-service/tests`
- backend integration tests in [`tests/backend-integration/`](./tests/backend-integration/)

The integration suite is the best place to validate the intended runtime lifecycle:

- queued -> running -> completed
- retries and exponential backoff
- dead-letter behavior
- cancellation and redrive
- crash recovery and checkpoint resume
- multi-worker coordination

## Notes

- `CLAUDE.md` remains at the repo root for tool-facing project context.
- Local `.venv`, build output, caches, and logs are intentionally not part of the committed project structure.
- `.tmp/` is used for transient local runtime output such as E2E service logs.
