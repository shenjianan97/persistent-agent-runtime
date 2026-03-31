# Persistent Agent Runtime

Durable execution infrastructure for AI agents.

## Why This Exists

Running AI agents in production — not as chatbots, but as long-running multi-step tasks on remote compute — surfaces infrastructure problems that most agent frameworks don't address:

- **Crash recovery:** Agent tasks can run for minutes or hours across many LLM calls. On ephemeral compute (containers, spot instances), the process can be killed at any time. Without durable checkpointing, all completed steps are lost and must be re-executed — wasting time and LLM spend.

- **Execution visibility:** When agents run on remote workers instead of your terminal, you need per-step cost tracking, failure diagnostics, and execution history to understand what happened and why.

- **Failure management:** At scale, agent task failures need to be inspectable and actionable — structured dead-letter queues with redrive, not just a stack trace in logs.

This project is a portfolio implementation that tackles these problems end-to-end: a checkpoint-resume execution model (not deterministic replay), lease-based crash recovery, Langfuse-backed execution observability, and dead-letter with redrive — built as a working system with a Java API, Python worker, React console, and PostgreSQL backing store.

## Current Architecture

Phase 1 uses a database-as-queue model:

1. Clients submit a task through the API service.
2. The task is stored in PostgreSQL in `queued` state.
3. A worker claims the task with `FOR UPDATE SKIP LOCKED`.
4. The worker executes the LangGraph workflow and writes checkpoints to PostgreSQL.
5. Heartbeats extend the lease while work is in progress.
6. A reaper recovers expired leases and handles timeout/dead-letter transitions.

## Repository Layout

- [`docs/PROJECT.md`](./docs/PROJECT.md): project overview, phases, tradeoffs, and roadmap
- [`docs/design/`](./docs/design/): architecture and design documents
- [`docs/implementation_plan/`](./docs/implementation_plan/): implementation planning and progress
- [`services/api-service/`](./services/api-service/): Spring Boot API service
- [`services/console/`](./services/console/): React SPA for monitoring and controlling the runtime
- [`services/worker-service/`](./services/worker-service/): Python worker, checkpointer, executor, and tools
- [`tests/backend-integration/`](./tests/backend-integration/): cross-service integration tests (API + Worker + PostgreSQL, mocked LLMs)
- [`tests/e2e-langfuse/`](./tests/e2e-langfuse/): Langfuse integration E2E tests (connectivity, trace publishing, cost tracking)
- [`tests/fixtures/`](./tests/fixtures/): shared test infrastructure (Langfuse Docker Compose)
- [`experiments/langgraph/`](./experiments/langgraph/): proof-of-concept and validation work
- [`infrastructure/database/`](./infrastructure/database/): schema migrations and verification
- [`infrastructure/cdk/`](./infrastructure/cdk/): AWS CDK infrastructure (Task 8)

## Getting Started

### Prerequisites

- Java 21+, Python 3.11+, Node.js 18+, Docker

### Quick Start

```bash
# 1. Bootstrap the database (first time only)
make init

# 2. Configure environment
cp .env.localdev.example .env.localdev
# At least one LLM key: ANTHROPIC_API_KEY, OPENAI_API_KEY
# Optional: TAVILY_API_KEY (web_search tool)

# 3. Install and run
make install
make start          # single worker (default)
make start N=3      # or start with multiple workers
```

For detailed setup options, environment variables, timing configuration, and manual database setup, see [`docs/LOCAL_DEVELOPMENT.md`](./docs/LOCAL_DEVELOPMENT.md).

### Deploy to AWS

For a full AWS deployment (Aurora Serverless v2, ECS Fargate, internal ALB, SSM access), see [`infrastructure/README.md`](./infrastructure/README.md).

### Useful Entry Points

- API service: [`services/api-service/README.md`](./services/api-service/README.md)
- Console: [`services/console/README.md`](./services/console/README.md)
- Worker service: [`services/worker-service/README.md`](./services/worker-service/README.md)
- Backend integration tests: [`tests/backend-integration/README.md`](./tests/backend-integration/README.md)
- Database schema: [`infrastructure/database/README.md`](./infrastructure/database/README.md)

### Common Commands

```bash
make install             # install all dependencies
make start               # start all services (1 worker)
make start N=3           # start all services with 3 workers
make scale-worker N=5    # scale workers up or down to N
make stop                # stop all services
make status              # show service statuses
make test-langfuse-up    # start local Langfuse for testing
make test-langfuse-down  # stop local Langfuse stack
make test-langfuse-status # inspect local Langfuse containers
make test-e2e-langfuse   # run Langfuse E2E tests (requires Langfuse + full stack)
make check               # verify prerequisites without starting services
make logs                # tail background service logs
make api-test            # API service tests
make worker-test         # worker service tests
make e2e-test            # backend integration tests
make db-reset-verify     # reset and verify database schema (destructive)
make clean               # remove build artifacts
```

Tip: use `make -n <target>` to preview the shell commands for a target without executing them. For example, `make -n start N=3` shows the full startup flow for three workers.

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
- Dynamic model provider management: database-backed provider/model registry, auto-discovery from API keys, per-model cost tracking, and console model selector via `GET /v1/models`
- Console frontend: dashboard, task list, task dispatcher, execution telemetry, dead letter queue, Langfuse endpoint settings
- Customer-owned Langfuse integration: per-task Langfuse endpoint configuration, CRUD API, connectivity testing, checkpoint-based cost/token aggregation
- End-to-end test coverage for crash recovery and lifecycle behavior

AWS deployment (validated end-to-end):

- CDK infrastructure: VPC, Aurora Serverless v2, ECS Fargate, internal ALB, SSM access host
- Automated schema bootstrap and model discovery on deploy
- Full deployment walkthrough: see [`infrastructure/README.md`](./infrastructure/README.md)

Still evolving:

- Later-phase multi-agent scheduling and budget enforcement

## Design Documents

Start here if you want the actual system contract rather than the repo overview:

- [`docs/design/phase-1/PHASE1_DURABLE_EXECUTION.md`](./docs/design/phase-1/PHASE1_DURABLE_EXECUTION.md)
- [`docs/design/phase-2/PHASE2_MULTI_AGENT.md`](./docs/design/phase-2/PHASE2_MULTI_AGENT.md)
- [`docs/design/phase-3-plus/DESIGN_NOTES_PHASE3_PLUS.md`](./docs/design/phase-3-plus/DESIGN_NOTES_PHASE3_PLUS.md)

For implementation planning:

- [`docs/implementation_plan/phase-1/plan.md`](./docs/implementation_plan/phase-1/plan.md)
- [`docs/implementation_plan/phase-1/progress.md`](./docs/implementation_plan/phase-1/progress.md)

## Testing

There are four practical test layers in the repo:

- API service tests in `services/api-service`
- worker service tests in `services/worker-service/tests`
- backend integration tests in [`tests/backend-integration/`](./tests/backend-integration/)
- Langfuse E2E tests in [`tests/e2e-langfuse/`](./tests/e2e-langfuse/) (requires local Langfuse via `make test-langfuse-up`)

The integration suite is the best place to validate the intended runtime lifecycle:

- queued -> running -> completed
- retries and exponential backoff
- dead-letter behavior
- cancellation and redrive
- crash recovery and checkpoint resume
- multi-worker coordination

### Local Validation Workflow

For local end-to-end validation of the `Makefile` workflow:

1. Run `make check` to confirm prerequisites and local dependencies.
2. Use `make db-migrate` for safe schema setup, or `make db-reset-verify` if you explicitly want a destructive reset plus schema verification.
3. Start the stack with `make start` or `make start N=3`.
4. Confirm the stack is healthy with `make status`, `curl http://localhost:8080/actuator/health`, and optionally `curl -I http://localhost:5173`.
5. Tail logs with `make logs` if startup looks suspicious.
6. Stop the stack with `make stop` when finished.

`make start` waits for the console, API, and requested worker count to come up before reporting success.

For code changes during local development, `make start` is source-based rather than build-artifact-based:

- Console runs the Vite dev server, so frontend edits hot-reload without a rebuild.
- API runs `./gradlew bootRun`, so a separate `./gradlew build` is not required before startup, but you do need to restart the API process to pick up Java code changes.
- Worker runs `python main.py` from source, so a separate build is not required, but you do need to restart worker processes to pick up Python code changes.

If services are already running, re-running `make start` does not restart them. Use `make restart` or the service-specific stop/start targets when you want local code changes to take effect for the API or worker.

When validating background service management, prefer running `make start`, `make status`, and `make stop` from a real interactive terminal. Some non-interactive runners can reap child processes when the parent command exits, which makes background-service checks look misleading.

## Notes

- `CLAUDE.md` remains at the repo root for tool-facing project context.
- Local `.venv`, build output, caches, and logs are intentionally not part of the committed project structure.
- `.tmp/` is used for transient local runtime output such as E2E service logs.
