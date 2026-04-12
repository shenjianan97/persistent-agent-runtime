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

Langfuse is configured per-agent via the Console Settings page. A local Langfuse instance is available for testing but is **not** started automatically by `make start`.

- `make test-langfuse-up` — start a local Langfuse Docker stack
- `make test-langfuse-status` — inspect Langfuse container status
- `make test-langfuse-down` — stop the local Langfuse stack

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
infrastructure/database/migrations/0004_timeout_reference.sql
infrastructure/database/migrations/0005_agents_table.sql
infrastructure/database/migrations/0006_runtime_state_model.sql
infrastructure/database/migrations/0007_scheduler_and_budgets.sql
```

To reset and re-verify the schema (destructive — drops all data):

```bash
make db-reset-verify
```

## E2E Tests (Isolated Infrastructure)

E2E tests run against **fully isolated infrastructure** that does not interfere with local development:

| Resource   | Local Dev          | E2E Tests                    |
|------------|--------------------|------------------------------|
| Postgres   | `:55432`           | `:55433`                     |
| API        | `:8080`            | `:8081`                      |
| DB name    | `persistent_agent_runtime` | `persistent_agent_runtime_e2e` |
| Container  | `persistent-agent-runtime-postgres` | `par-e2e-postgres` |

### Running E2E tests

```bash
# Run E2E tests (auto-starts isolated Postgres + API if needed)
make e2e-test

# Or manage E2E infra manually
make e2e-up        # start isolated DB + API
make e2e-status    # check what's running
make e2e-down      # stop E2E stack

# If something failed mid-run, force-clean leftovers
make e2e-clean
```

`make e2e-test` uses `-v --tb=short -ra` for verbose progress and failure details. Logs are written to `.tmp/e2e-test.log` and `.tmp/e2e-api-service.log`.

### Test targets

- `make test` — unit tests only (API, Worker, Console). Fast, no infrastructure needed.
- `make test-all` — unit tests + E2E. Full validation.
- `make e2e-test` — E2E only. Auto-manages its own infrastructure.

## Testing Rules

### Every code change requires tests

If you change code, you must add or update tests that cover the change — unless the change is purely cosmetic (e.g., comments, formatting) or the existing test suite already covers the new behavior. Test all use cases and failure scenarios, not just the happy path.

### Where to put tests

| Service | Test directory | Framework | Example |
|---------|---------------|-----------|---------|
| API (Spring Boot) | `services/api-service/src/test/java/...` | JUnit 5 | `AgentControllerTest.java` |
| Worker (Python) | `services/worker-service/tests/` | pytest | `test_poller.py`, `test_executor.py` |
| Console (React) | Co-located `*.test.tsx` / `*.test.ts` | Vitest | `src/features/agents/AgentDetailPage.test.tsx` |
| E2E (integration) | `tests/backend-integration/` | pytest (async) | `test_budget_enforcement.py` |

Follow existing conventions in each directory. Place unit tests next to the code they test (console) or in the flat `tests/` directory (worker).

### Running a single test

Use these commands to run individual tests when debugging failures. Always use the project venv for Python.

**Worker (Python):**
```bash
# Single file
services/worker-service/.venv/bin/python -m pytest services/worker-service/tests/test_poller.py -v

# Single test function
services/worker-service/.venv/bin/python -m pytest services/worker-service/tests/test_poller.py::test_function_name -v

# Single test class method
services/worker-service/.venv/bin/python -m pytest services/worker-service/tests/test_poller.py::TestClassName::test_method -v

# With print output visible
services/worker-service/.venv/bin/python -m pytest services/worker-service/tests/test_poller.py -v -s
```

**E2E (Python):**
```bash
# Single file
services/worker-service/.venv/bin/python -m pytest tests/backend-integration/test_budget_enforcement.py -v -s

# Single test
services/worker-service/.venv/bin/python -m pytest tests/backend-integration/test_budget_enforcement.py::TestBudgetEnforcement::test_per_task_budget_pause -v -s
```

**API (Gradle/JUnit):**
```bash
cd services/api-service

# Single test class
./gradlew test --tests "com.persistentagent.api.controller.AgentControllerTest"

# Single test method
./gradlew test --tests "com.persistentagent.api.controller.AgentControllerTest.methodName"
```

**Console (Vitest):**
```bash
cd services/console

# Single file
npx vitest run src/features/agents/AgentDetailPage.test.tsx

# Filter by test name
npx vitest run --reporter=verbose -t "renders budget fields"
```

### Infrastructure prerequisites for tests

**All tests use an isolated test database — never the local dev database.** Worker integration tests and E2E tests both connect to a dedicated test PostgreSQL container (`par-e2e-postgres`) on port **55433**, separate from the local dev DB on port 55432. This ensures tests never destroy your local development data.

`make worker-test` automatically starts the test database container and applies migrations via the `test-db-up` dependency. If you see `N passed, 12 skipped` in worker test output, it means Docker is not running — start Docker Desktop and re-run.

`make e2e-test` also depends on the test database (via `e2e-up → test-db-up`) plus an API service on port 8081. LocalStack on port 4566 must be running for artifact-related E2E tests — `make db-up` starts both the dev PostgreSQL and LocalStack containers.

### Pre-existing test failures

If tests fail that are unrelated to your change, you must still fix them. Use `git stash` or a separate branch to verify the failure exists on `main` before your changes. If it does, fix it as part of your work — do not leave broken tests behind.

## Browser-Based Console Verification

Automated console tests (Vitest + `@testing-library/react`) verify component rendering in isolation with mocked data. They cannot catch routing bugs, layout issues, or broken user flows that only appear in a real browser with live API data.

For console UI changes, use Playwright MCP browser tools to verify the UI end-to-end against a running instance:

- **Prerequisites:** `make start` must be running (Console at `http://localhost:5173`, API at `http://localhost:8080`)
- **Scenarios:** See [`docs/CONSOLE_BROWSER_TESTING.md`](./CONSOLE_BROWSER_TESTING.md) for standard verification scenarios
- **Tools:** `browser_navigate`, `browser_snapshot` for structural assertions, `browser_click`, `browser_take_screenshot` for visual checks, and `browser_console_messages` for error detection
- **When:** After implementing any console-facing change, run the relevant scenarios from the matrix in the scenarios doc
