# End-to-End Test Suite

This directory contains the **Phase 1 durable execution** E2E test suite for the persistent agent runtime.

The tests validate the full runtime path:
- API Service (Java/Spring)
- Worker Service (Python/asyncio + LangGraph)
- PostgreSQL (queue/state/checkpoints)

All scenarios from [`end-to-end-plan.md`](docs/implementation_plan/phase-1/testing/end-to-end-plan.md) are implemented.

## What This Suite Covers

- Task lifecycle: `queued -> running -> completed/dead_letter`
- Retry and backoff behavior
- Cancellation and redrive
- Reaper lease recovery and timeout handling
- Checkpoint ordering and resume behavior
- Multi-worker coordination and concurrency
- API validation and tenant isolation

## Test Design

The suite uses:
- `pytest` + `pytest-asyncio`
- Real API + DB
- In-process worker startup
- Deterministic LLM mocks by patching `executor.graph.ChatAnthropic`

Common test behavior is centralized in:
- [`helpers/e2e_context.py`](tests/e2e/helpers/e2e_context.py)
- [`conftest.py`](tests/e2e/conftest.py)

Use the `e2e` fixture in tests for:
- setting LLM behavior
- starting/stopping workers
- task submission/status polling
- DB assertions

## Infrastructure Behavior (Hybrid Detect-and-Reuse)

By default, the suite:
1. Reuses running services when available.
2. Starts missing services when needed.

Specifically:
- If PostgreSQL is not reachable on `localhost:55432`, tests may start Docker container `par-e2e-postgres`.
- If API health is not available on `http://localhost:8080/v1/health`, tests may start `services/api-service` via `./gradlew bootRun`.
- If schema is missing, migration `infrastructure/database/migrations/0001_phase1_durable_execution.sql` is applied.

## Prerequisites

- Docker
- `psql`
- Java 21+
- Python environment with worker dependencies (recommended: `services/worker-service/.venv`)

## Run Tests

From repository root:

```bash
services/worker-service/.venv/bin/python -m pytest -q tests/e2e
```

Run a single file:

```bash
services/worker-service/.venv/bin/python -m pytest -q tests/e2e/test_recovery.py
```

Run a single test:

```bash
services/worker-service/.venv/bin/python -m pytest -q tests/e2e/test_crash_resume.py::test_3_19_crash_recovery_node_resume_boundary
```

## Environment Variables

You can override runtime defaults with:

- `E2E_DB_HOST` (default: `localhost`)
- `E2E_DB_PORT` (default: `55432`)
- `E2E_DB_NAME` (default: `persistent_agent_runtime`)
- `E2E_DB_USER` (default: `postgres`)
- `E2E_DB_PASSWORD` (default: `postgres`)
- `E2E_DB_DSN` (overrides all DB pieces)
- `E2E_API_PORT` (default: `8080`)
- `E2E_API_BASE` (default: `http://localhost:8080/v1`)
- `E2E_PG_CONTAINER` (default: `par-e2e-postgres`)
- `E2E_PG_IMAGE` (default: `postgres:16`)
- `E2E_SKIP_AUTO_INFRA=1` (disable auto start/reuse logic)

## Troubleshooting

- If tests hang on status waits, check API and worker logs first.
- If DB logs are noisy, it is usually due to status polling loops in tests.
- If you already run PostgreSQL in container `persistent-agent-runtime-postgres` on port `55432`, the suite will reuse it.
