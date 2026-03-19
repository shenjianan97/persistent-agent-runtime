# Backend Integration Test Suite

Cross-service integration tests for the **Phase 1 durable execution** runtime. Tests the full backend path — API Service (Java/Spring), Worker Service (Python/asyncio + LangGraph), and PostgreSQL — with **mocked LLMs** for deterministic, fast execution.

No frontend is involved.

## What This Suite Covers

- Task lifecycle: `queued -> running -> completed/dead_letter`
- Retry and backoff behavior
- Cancellation and redrive
- Reaper lease recovery and timeout handling
- Checkpoint ordering and resume behavior
- Multi-worker coordination and concurrency
- API validation and tenant isolation
- Dev task controls (`expire-lease`, `force-dead-letter`) and deterministic timeout testing via `dev_sleep`

## Test Design

- `pytest` + `pytest-asyncio`
- **Real** API service + PostgreSQL
- In-process worker startup (workers are created directly with tuned configs for fast test execution, not via `main.py`)
- **Mock LLMs** — all tests patch `executor.providers.create_llm` via `helpers/mock_llm.py`. There is no real LLM mode.

Common test behavior is centralized in:
- [`helpers/e2e_context.py`](helpers/e2e_context.py)
- [`conftest.py`](conftest.py)

Use the `e2e` fixture in tests for setting LLM behavior, starting/stopping workers, task submission/status polling, and DB assertions.

## Infrastructure Behavior (Hybrid Detect-and-Reuse)

By default, the suite:
1. Reuses running services when available.
2. Starts missing services when needed.

Specifically:
- If PostgreSQL is not reachable on `localhost:55432`, tests may start Docker container `par-e2e-postgres`.
- If API health is not available on `http://localhost:8080/v1/health`, tests may start `services/api-service` via `./gradlew bootRun` with `APP_DEV_TASK_CONTROLS_ENABLED=true`.
- If schema is missing, all SQL files under `infrastructure/database/migrations/` are applied in order.

## Prerequisites

- Docker
- `psql`
- Java 21+
- Python environment with worker dependencies (recommended: `services/worker-service/.venv`)

## Run Tests

From repository root:

```bash
services/worker-service/.venv/bin/python -m pytest -q tests/backend-integration
```

Run a single file:

```bash
services/worker-service/.venv/bin/python -m pytest -q tests/backend-integration/test_recovery.py
```

Run the dev task controls scenarios only:

```bash
services/worker-service/.venv/bin/python -m pytest -q tests/backend-integration/test_dev_task_controls.py
```

Run a single test:

```bash
services/worker-service/.venv/bin/python -m pytest -q tests/backend-integration/test_crash_resume.py::test_3_19_crash_recovery_node_resume_boundary
```

## Environment Variables

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

The suite enables API dev task controls automatically for any API instance it starts itself.

Allowed dead-letter reasons for the dev task-control endpoint:
- `cancelled_by_user`
- `retries_exhausted`
- `task_timeout`
- `non_retryable_error`
- `max_steps_exceeded`

## Troubleshooting

- If tests hang on status waits, check API and worker logs first.
- If DB logs are noisy, it is usually due to status polling loops in tests.
- If you already run PostgreSQL in container `persistent-agent-runtime-postgres` on port `55432`, the suite will reuse it.
