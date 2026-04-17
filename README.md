# Persistent Agent Runtime

Durable execution infrastructure for AI agents.

## Why This Exists

Running AI agents in production — not as chatbots, but as programmatic, multi-step tasks on remote compute — surfaces infrastructure problems that most agent frameworks don't address:

- **Crash recovery:** Agent tasks can run for minutes or hours across many LLM calls. On ephemeral compute (containers, spot instances), the process can be killed at any time. Without durable checkpointing, all completed steps are lost and must be re-executed — wasting time and LLM spend.

- **Execution visibility:** When agents run on remote workers instead of your terminal, you need per-step cost tracking, failure diagnostics, and execution history to understand what happened and why.

- **Failure management:** At scale, agent task failures need to be inspectable and actionable — structured dead-letter queues with redrive, not just a stack trace in logs.

This project is developer infrastructure for deploying agents at scale — the layer between "I have an agent" and "I can run 500 of them reliably in production." Target use cases are coding agents, batch document processing, and research tasks.

Key capabilities:

- **Checkpoint-resume execution** — Postgres-backed LangGraph checkpointer; tasks survive worker crashes at node boundaries (not deterministic replay)
- **Lease-based crash recovery** — any worker can resume any task; a reaper recovers expired leases
- **Multi-provider LLM** — Anthropic, OpenAI, Google, and AWS Bedrock, auto-discovered from configured API keys
- **Per-agent cost tracking and budgets** — incremental per-step cost, budget-based task pausing
- **Human-in-the-loop** — `waiting_for_approval` and `waiting_for_input` states with stateless pause/resume
- **Customer-owned observability** — per-task Langfuse endpoints, checkpoint-based cost/token aggregation
- **Dead-letter with redrive** — structured failure inspection, not log scraping
- **Bring-your-own-tools (BYOT)** — customer-provided MCP tool servers registered by URL
- **Sandboxed code execution** — E2B-backed shell, file I/O, and artifact export; file input via multipart upload

Built as a working system with a Java/Spring Boot API, Python worker, React/Vite console, and PostgreSQL state store, deployable on ECS Fargate + Aurora Serverless v2.

## Current Architecture

The system uses a stateless worker pool with a database-as-queue model. Workers are interchangeable — any worker can claim any task, and any worker can resume a checkpointed task from another worker. State lives in PostgreSQL (shared), not on local disk, so tasks survive crashes, deployments, and scaling events.

1. Clients submit a task through the API service, targeting a configured agent.
2. The task is stored in PostgreSQL in `queued` state.
3. A worker claims the task with `FOR UPDATE SKIP LOCKED`.
4. The worker loads the agent's configuration (model, tools, system prompt) and executes the LangGraph workflow, writing checkpoints to PostgreSQL.
5. Heartbeats extend the lease while work is in progress.
6. Tasks can pause for human approval or input (`waiting_for_approval`, `waiting_for_input`), releasing the worker. Any worker can resume when the human responds.
7. A reaper recovers expired leases and handles timeout/dead-letter transitions.

## Built-in Agent Tools

Agents have access to these tools at runtime, wired directly into the LangGraph executor. Actual availability per task depends on the agent's `allowed_tools` config and whether a sandbox is provisioned:

- `web_search` — web search via DuckDuckGo
- `read_url` — fetch and extract text from a public URL
- `request_human_input` — pause the task and wait for a human operator response
- `create_text_artifact` — save text output as a downloadable task artifact (S3). Available only to **non-sandbox agents**; sandbox-enabled agents use `export_sandbox_file` for output files instead
- `sandbox_exec`, `sandbox_read_file`, `sandbox_write_file`, `export_sandbox_file` — shell and file I/O inside an E2B sandbox (available when the agent is configured with `sandbox.enabled: true`)

Agents configured with a **custom MCP tool server URL** (BYOT) get the server's tools merged into the same tool list at task start.

## Repository Layout

- [`docs/product-specs/`](./docs/product-specs/): vision, user stories, core concepts
- [`docs/design-docs/`](./docs/design-docs/): architecture and design documents
- [`docs/exec-plans/`](./docs/exec-plans/): implementation plans (active and completed)
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

### Phase 1 — Durable Execution (complete)

- Database schema with lease-based task claiming (`FOR UPDATE SKIP LOCKED`)
- REST API for task submission, listing, status, checkpoints, cancellation, dead-letter listing, and redrive
- Worker poller, heartbeat manager, and reaper
- Worker registry with self-registration, heartbeat, and stale worker cleanup
- PostgreSQL-backed LangGraph checkpointer
- Built-in tools: `web_search`, `read_url`, `request_human_input`, plus a standalone FastMCP server exposing `web_search`, `read_url`, `calculator` for external use
- Dev-only task controls for forced lease expiry and dead-letter transitions
- Dynamic model provider management: database-backed provider/model registry, auto-discovery from API keys, per-model cost tracking
- Console frontend: dashboard, task list, task dispatcher, execution telemetry, dead letter queue
- End-to-end test coverage for crash recovery and lifecycle behavior

### Phase 2 — Multi-Agent (in progress)

Completed tracks:

- **Track 1 — Agent Control Plane:** Agent as first-class entity with CRUD, configuration management, and per-agent task routing
- **Track 2 — Runtime State Model:** Human-in-the-loop workflows (`waiting_for_approval`, `waiting_for_input`), stateless pause/resume, task event history
- **Track 3 — Scheduler and Budgets:** Agent-aware round-robin scheduling, per-agent budgets, budget-based task pausing, incremental cost tracking
- **Track 4 — Custom Tool Runtime (BYOT):** Customer-provided MCP tool servers registered by URL, worker-side MCP client integration, bearer token auth

Cross-cutting (completed):

- **Customer-owned Langfuse integration:** per-task Langfuse endpoint configuration, CRUD API, connectivity testing, checkpoint-based cost/token aggregation
- **Agent Capabilities:** E2B sandbox integration (provision / pause / resume / destroy lifecycle, crash recovery via `sandbox_id` reconnect), sandbox tools (`sandbox_exec`, `sandbox_read_file`, `sandbox_write_file`, `export_sandbox_file`), output artifact storage on S3 (LocalStack locally), `create_text_artifact` tool, and multipart file-input task submission — see [`docs/design-docs/agent-capabilities/design.md`](./docs/design-docs/agent-capabilities/design.md)

Upcoming:

- **Track 5 — Memory:** long-term memory extraction, append-only storage, compaction
- **Track 6 — GitHub Integration:** code agent input/output via pull requests

### AWS Deployment (validated end-to-end)

- CDK infrastructure: VPC, Aurora Serverless v2, ECS Fargate, internal ALB, SSM access host
- Automated schema bootstrap and model discovery on deploy
- Full deployment walkthrough: see [`infrastructure/README.md`](./infrastructure/README.md)

## Design Documents

Start here if you want the actual system contract rather than the repo overview:

- [`docs/design-docs/core-beliefs.md`](./docs/design-docs/core-beliefs.md) — architectural invariants governing all phases
- [`docs/design-docs/phase-1/design.md`](./docs/design-docs/phase-1/design.md) — durable execution foundation
- [`docs/design-docs/phase-2/design.md`](./docs/design-docs/phase-2/design.md) — multi-agent, memory, scheduling, custom tools
- [`docs/design-docs/agent-capabilities/design.md`](./docs/design-docs/agent-capabilities/design.md) — sandbox, artifacts, file input
- [`docs/design-docs/langfuse/design.md`](./docs/design-docs/langfuse/design.md) — customer-owned Langfuse integration
- [`docs/design-docs/phase-3-plus/design-notes.md`](./docs/design-docs/phase-3-plus/design-notes.md) — future reference material

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

- `AGENTS.md` is the primary agent-facing navigation file. `CLAUDE.md` redirects to it.
- Local `.venv`, build output, caches, and logs are intentionally not part of the committed project structure.
- `.tmp/` is used for transient local runtime output such as E2E service logs.
