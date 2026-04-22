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
- forwards `APP_DEV_TASK_CONTROLS_ENABLED` (default `true` for local dev) to the API, worker, and console, and `VITE_DEV_TASK_CONTROLS_ENABLED` (derived from it) to the console
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

## Tracking a running task

Three surfaces answer "what is this task doing?" — pick by the question you're asking:

| Your question | Surface | How to get there |
|---|---|---|
| What did the **agent** do? (turns, tool calls, results) | Conversation API | `GET /v1/tasks/<id>/conversation` |
| What is the **runtime** deciding? (lifecycle, pause/retry/dead-letter, compaction) | Worker log | `.tmp/worker-<N>.log` |
| Per-LLM-call traces and token costs | Langfuse UI | See [Local Langfuse Observability](#local-langfuse-observability) above |

**Prerequisites.** `make start` running; `jq` installed (`brew install jq`). The API defaults to `http://localhost:8080` — override with `API_PORT`.

### Quick start — "I submitted a task, now what?"

1. **Find the task ID.** The console prints it on submit; otherwise list the most recent:

   ```bash
   curl -s http://localhost:8080/v1/tasks | jq '.items[] | {id: .task_id, status, agent: .agent_display_name, created_at}'
   ```

2. **See what the agent did** — the conversational transcript (turns, tool calls, tool results, HITL resumes):

   ```bash
   curl -s http://localhost:8080/v1/tasks/<task-id>/conversation \
     | jq '.entries[] | {seq: .sequence, kind, tool: .content.tool_name, size: .content_size}'
   ```

3. **See what the runtime decided** — tail the worker log. `<N>` is the worker number (`1..WORKER_COUNT`); `ls .tmp/worker-*.log` to see which exist. The `fromjson?` wrapper is required because the worker log mixes JSON event lines with stdlib text lines — without it `jq` would parse-error on the text lines:

   ```bash
   tail -f .tmp/worker-1.log | jq -Rc --arg id "<task-id>" 'fromjson? | select(.task_id==$id) | {t: .timestamp, event, level}'
   ```

4. **If something looks off**, check the task-level status and the structured event feed:

   ```bash
   curl -s http://localhost:8080/v1/tasks/<task-id> | jq '{status, retry_count, output}'
   curl -s http://localhost:8080/v1/tasks/<task-id>/events | jq '.events[] | {t: .created_at, event_type, status_after, error_code}'
   ```

### Top events to know

Watch these in the worker log — each maps to a concrete question you're likely to ask.

| Event | Level | Read when |
|---|---|---|
| `TASK_CLAIMED` / `TASK_COMPLETED` | INFO | "Did my task start / finish?" |
| `Task <id> paused: <reason> (...)` | INFO | "Why is it stuck?" Usual reasons: `hitl_approval`, `hitl_input_requested`, `budget_exceeded` |
| `Task <id> hit retryable error. Requeued (try N).` | INFO | "It retried — was it transient?" |
| `Task <id> dead-lettered: <reason> (msg: ...)` | ERROR | **Look here first for terminal failures.** The `reason` code plus the `msg` string is usually enough to triage. |
| `compaction.tier3_fired` | INFO | "Did context get compacted?" Explains surprising "the agent forgot X" behaviour. |

> **Glossary.** HITL = human-in-the-loop pause (task waiting on approval or user input). Tier‑3 = the aggressive context-compaction pass that summarises older history to stay inside the model's token budget. Hard floor = the token ceiling beyond which compaction refuses to run further.

### Peeking inside compaction

`make start` runs workers at `WORKER_LOG_LEVEL=DEBUG` by default, so the per-turn compaction trace is already in your log — no extra flags. Filter for the `compaction.projection_built` event, emitted once per LLM call:

```bash
jq -Rc 'fromjson? | select(.event=="compaction.projection_built" and .task_id=="<task-id>")
                  | {t: .timestamp, est: .est_tokens, trigger: .trigger_tokens, outcome}' \
   .tmp/worker-1.log
```

> **Seeing lots of `below_threshold`? That's healthy.** It means the hook is checking every turn and finding nothing to do — current tokens are comfortably under the trigger (0.85 × `model_context_window`).

Outcomes you might see: `below_threshold`, `fired`, `flush_fired`, `skipped:cap_reached`, `skipped:empty_slice`, `skipped:fatal`, `skipped:empty_summary`, `fatal_short_circuit`. Any of them may be suffixed `:hard_floor` when the projection still exceeds the model window.

**Quieting the log.** If DEBUG is too chatty, override for a specific run: `WORKER_LOG_LEVEL=INFO make start-worker`. Accepted values: `DEBUG` (Makefile default), `INFO` (code / production default), `WARNING`, `ERROR`, `CRITICAL` (case-insensitive; invalid values fall back to INFO). `WORKER_LOG_LEVEL` is worker-only; the API service has separate logging.

### Full event reference

The worker log mixes two formats:

- **JSON event lines** (lines starting with `{`) — structured events via structlog. Grep with `jq`.
- **Text lines** (lines starting with a timestamp like `2026-04-20 15:43:...`) — stdlib `logging` output. Grep with plain `grep`.

To separate them:

```bash
jq -Rc 'fromjson? | select(.event != null)' .tmp/worker-1.log   # JSON events only
grep -v '^{' .tmp/worker-1.log                                  # text lines only
```

Tip: `jq -R 'fromjson?'` reads each line as raw text, tries to parse it as JSON, and silently skips lines that aren't — handy for grepping the mixed log without parse errors.

**Lifecycle (JSON, INFO):** `TASK_CLAIMED`, `TASK_COMPLETED`, `TASK_REQUEUED`, `TASK_DEAD_LETTERED`, `LEASE_REVOKED`, `HEARTBEAT_SENT`, `POLL_EMPTY`, `REAPER_LEASE_EXPIRED`, `REAPER_TASK_TIMEOUT`, `REAPER_DEAD_LETTERED`.

**Compaction (JSON):**
- `compaction.projection_built` — DEBUG, one per hook invocation (see [Peeking inside compaction](#peeking-inside-compaction-opt-in-debug)).
- `compaction.tier3_fired` / `compaction.tier3_skipped` / `compaction.hard_floor` — INFO/WARN. Fields include `tokens_in`, `tokens_out`, `summarizer_model_id`.
- `compaction.model_context_window_unknown` — WARN at task start when the model's context window is unresolved; worker falls back to 128,000 tokens. Usually means a model row is missing its `context_window` value.

**Runtime text lines (stdlib, grep by substring):**
- `memory.route_after_agent` — per-turn routing for memory-enabled agents (`decision=tools`/`end`).
- `Task <id> paused: <reason> (cost: ...)` / `Task <id> paused: <reason> (timeout: ...)` — HITL or budget pause.
- `Task <id> hit retryable error. Requeued (try N).` — retry loop.
- `Task <id> dead-lettered: <reason> (msg: <detail>)` — terminal failure.
- `Per-step cost recording failed` / `Execution metadata write failed` — DB write hiccups; the task keeps running.
- `sandbox_timeout_refresh_failed` — sandbox heartbeat miss at DEBUG; benign unless it recurs.
- `Langfuse endpoint ... not found` / `Langfuse flush failed` — per-task tracing degraded; task is unaffected.

**Two handy cross-task recipes:**

```bash
# All lifecycle transitions across every worker
jq -Rc 'fromjson? | select(.event | IN("TASK_CLAIMED","TASK_COMPLETED","TASK_REQUEUED","TASK_DEAD_LETTERED","LEASE_REVOKED"))' \
   .tmp/worker-*.log

# All text-format lines for one task
grep '<task-id>' .tmp/worker-*.log | grep -v '^{'
```

### Don't ship DEBUG to production

The worker service defaults to `INFO` in code (see `services/worker-service/core/logging.py`). DEBUG is enabled *only* by the Makefile's `WORKER_LOG_LEVEL ?= DEBUG`, which applies to `make start` / `make start-worker` / `make scale-worker`. Production deploys (Helm, CDK) must not set `WORKER_LOG_LEVEL` — at fleet scale DEBUG produces several MB of JSON per hour per worker.

### Disabling prompt-cache markers (operator kill switch)

Prompt caching is on by default — the worker adds provider-specific cache markers (`cache_control` for Anthropic, `cachePoint` for Bedrock Converse) to every LLM request so multi-turn agent loops cache their stable prefix. There is **no per-agent toggle** by design: disabling caching only raises costs, so the knob is deliberately not exposed as customer config.

The `WORKER_PROMPT_CACHE_DISABLED` env var is an **operator-only emergency lever** — use it when a provider caching regression, a suspected SDK bug, or a debugging need requires suppressing markers without touching the database. The Makefile forwards it:

```bash
# Kill switch for this session — all workers start with markers suppressed
WORKER_PROMPT_CACHE_DISABLED=1 make start

# Restart just the workers with caching off, keeping the rest of the stack up
WORKER_PROMPT_CACHE_DISABLED=1 make restart
```

Accepted truthy values (case-insensitive): `1`, `true`, `yes`, `on`. Anything else (including unset, empty, `0`, `false`) keeps caching enabled.

Token-usage extraction is intentionally **not** gated on this flag — OpenAI caches prefixes automatically regardless of whether we inject markers, and we must keep extracting `cached_tokens` from their response metadata to avoid mis-reporting cached reads as regular input tokens (which would over-attribute spend by ~10× on cached turns).

When the kill switch is active, the worker emits a single `prompt_cache.markers_disabled_via_env` warning at startup so it's clear in logs that markers are off on purpose.

## Dev-Only Task Controls

The runtime includes dev-only endpoints and tools for testing failure and recovery flows locally:

- `POST /v1/dev/tasks/{taskId}/expire-lease` — forces a running task into lease-expiry recovery (simulates a worker crash)
- `POST /v1/dev/tasks/{taskId}/force-dead-letter` — forces a task into the dead-letter path
- `dev_sleep` tool — an agent tool that sleeps for a configurable duration, useful for exercising timeout and long-running-task behavior deterministically

**These are enabled by default under `make start`** — the Makefile sets `APP_DEV_TASK_CONTROLS_ENABLED ?= true` because local development is the Makefile's only audience. Production deploys (Helm, CDK, Lambda) don't invoke the Makefile and therefore see each service's code-level default of `false` when the env var is unset.

To explicitly opt out for a local run:

```bash
APP_DEV_TASK_CONTROLS_ENABLED=false make start
```

## Timing Configuration

For faster local recovery testing, you can shorten the lease, heartbeat, and reaper timings (dev task controls are already on under `make start`):

```bash
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
