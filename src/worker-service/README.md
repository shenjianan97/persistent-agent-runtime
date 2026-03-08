# Worker Service

The worker service now includes the foundational asyncio primitives for claiming, leasing, and recycling tasks in the Persistent Agent Runtime plus the Phase 1 co-located MCP server. It implements the database-as-queue pattern from the Phase 1 design using PostgreSQL `FOR UPDATE SKIP LOCKED`, with lease-based ownership to guarantee that no two workers execute the same task simultaneously.

The worker core remains intentionally free of LangGraph orchestration logic. It exports reusable primitives that are consumed by the `GraphExecutor` (implemented in the `executor/` module as part of Task 6), while `tools/` exposes the read-only FastMCP server contract that `GraphExecutor` dispatches through.

## Architecture

Three subsystems run concurrently inside a single asyncio event loop:

```
 WorkerService
 |
 |-- TaskPoller
 |     - LISTEN new_task (primary wake)
 |     - FOR UPDATE SKIP LOCKED claim query
 |     - Exponential backoff on empty polls (100ms -> 5s cap)
 |     - asyncio.Semaphore bounds concurrency to MAX_CONCURRENT_TASKS
 |     - Coordinates `TaskRouter` and `HeartbeatManager` per task
 |
 |-- HeartbeatManager
 |     - One asyncio task per active task, every 15s
 |     - Extends lease_expiry by 60s
 |     - Detects lease revocation (UPDATE returns 0 rows)
 |     - Sets HeartbeatHandle.cancel_event so the executor can stop
 |
 |-- ReaperTask
 |     - Runs on every worker instance (not a singleton)
 |     - Jittered interval: 30s +/- 10s
 |     - Expired leases: requeue (retry_count < max_retries) or dead-letter
 |     - Task timeouts: dead-letter with reason 'task_timeout'
 |     - Emits pg_notify('new_task', pool_id) in the same transaction
 |
 |-- TaskRouter (Injected)
 |     - Inspects `task_data` to decide which TaskExecutor to use
 |
 |-- TaskExecutor (Injected, e.g. GraphExecutor)
       - Runs LangGraph compilation and LLM generation
       - Boots in-memory MCP Server to expose tools (calculator, web_search, etc.)
```

### How a Task flows through the Subsystems

1. **Revervation:** `ReaperTask` runs in the background. If another worker crashes, it finds tasks where `lease_expiry` is in the past and resets them to `queued`.
2. **Claiming:** `TaskPoller` waits for Postgres `LISTEN/NOTIFY` events. When a new task appears, it runs a `FOR UPDATE SKIP LOCKED` query to claim it, giving the worker a 60-second lease.
3. **Coordination:** Inside the Poller, it immediately asks the `HeartbeatManager` to start pinging Postgres every 15s in the background to keep the lease alive. 
4. **Routing:** The Poller hands the task to the `TaskRouter`, which decides that the `GraphExecutor` should handle it.
5. **Execution:** The `GraphExecutor` boots the local MCP server to register available tools (`calculator`, `web_search`), compiles the LangGraph state machine, and streams events from Anthropic.
6. **Cancellation:** If the `HeartbeatManager` fails to renew the lease (e.g. DB goes down), it sets a `cancel_event` flag. The `GraphExecutor` checks this flag between LLM steps and gracefully aborts if the lease is lost.
7. **Completion:** When the graph finishes, the executor writes the final `completed` status to the DB, and the Poller tells the `HeartbeatManager` to stop pinging.

All SQL queries are taken verbatim from `design/PHASE1_DURABLE_EXECUTION.md` Section 6.1. All reaper operations use `UPDATE ... RETURNING` to avoid TOCTOU races between multiple reaper instances.

## Module Structure

```
core/
  __init__.py       Public API: WorkerConfig, TaskPoller, HeartbeatManager, ReaperTask, WorkerService
  config.py         WorkerConfig frozen dataclass with all tunable parameters
  db.py             asyncpg connection pool and dedicated LISTEN connection factory
  poller.py         TaskPoller: LISTEN/NOTIFY + FOR UPDATE SKIP LOCKED claim loop
  heartbeat.py      HeartbeatManager + HeartbeatHandle: per-task lease extension and revocation detection
  reaper.py         ReaperTask: distributed expired-lease and timeout scanner
  logging.py        Structured logging (structlog JSON), lifecycle event constants, MetricsCollector
  worker.py         WorkerService: top-level orchestrator, signal-based graceful shutdown
checkpointer/
  __init__.py       Public API: PostgresDurableCheckpointer, LeaseRevokedException
  postgres.py       Lease-aware LangGraph checkpoint saver backed by PostgreSQL
executor/
  __init__.py       Public API: GraphExecutor, DefaultTaskRouter, TaskRouter, TaskExecutor
  router.py         TaskExecutor / TaskRouter protocols, DefaultTaskRouter (Phase 1 always→GraphExecutor)
  graph.py          LangGraph assembly, integration with tools/checkpointer, and error classification
tools/
  __init__.py       Public API: create_mcp_server, create_tool_server_app, tool definitions, schema helpers
  app.py            Extractable FastMCP application assembly
  server.py         Worker-owned runtime shim and stdio/HTTP entrypoint
  definitions.py    Canonical tool names, schemas, models, and registration helpers
  env.py            `.env` loading for tool configuration
  runtime_logging.py stderr logging for MCP startup and tool calls
  sample_client.py  Manual HTTP client for local MCP testing
  calculator.py     Safe AST-based arithmetic evaluator
  read_url.py       Bounded URL fetch + HTML/text extraction with SSRF guards
  providers/
    search.py       SearchProvider protocol and Tavily-backed implementation
```

## Configuration

`WorkerConfig` is a frozen dataclass. All fields have sensible defaults:

| Field | Default | Description |
|-------|---------|-------------|
| `worker_id` | `worker-{hostname}-{pid}-{uuid8}` | Auto-generated unique identity |
| `worker_pool_id` | `"shared"` | Task routing key (Phase 2 multi-pool) |
| `tenant_id` | `"default"` | Tenant scope for all queries |
| `db_dsn` | `postgresql://localhost:5432/agent_runtime` | asyncpg connection string |
| `max_concurrent_tasks` | `10` | Semaphore bound per worker instance |
| `poll_backoff_initial_ms` | `100` | First backoff after empty poll |
| `poll_backoff_max_ms` | `5000` | Backoff cap |
| `poll_backoff_multiplier` | `2.0` | Backoff growth factor |
| `lease_duration_seconds` | `60` | Lease window set on claim |
| `heartbeat_interval_seconds` | `15` | How often heartbeat fires |
| `reaper_interval_seconds` | `30` | Base reaper scan interval |
| `reaper_jitter_seconds` | `10` | +/- jitter on reaper interval |

## Integration Point (Graph Executor)

The Graph Executor (`executor/graph.py`) plugs in through the `on_task_claimed` callback mechanism. It handles the entire LangGraph orchestration layer:

- **Graph Assembly**: Dynamically constructs a `StateGraph`, binds the specified LLM (Anthropic or Bedrock based on model name), and injects Phase 1 tools.
- **Durable Checkpointing**: Initialises the `PostgresDurableCheckpointer` to persist and resume graphs from the PostgreSQL `checkpoints` table.
- **Safety**: Wraps the graph execution in an `asyncio.timeout(task_timeout_seconds)` and limits steps using LangGraph's `max_steps`.
- **Failure Classification**: Distinguishes transient, retryable faults (e.g., rate limits, HTTP 5xx errors) from fatal faults and updates task status and exponential backoff timers dynamically.
- **Cancellation**: Halts the LangChain iteration if it detects the heartbeat `cancel_event` flag has been set on lease revocation.

Example Usage:
```python
from core.db import create_pool
from core.worker import WorkerService
from executor.router import DefaultTaskRouter

pool = await create_pool(config.db_dsn)
router = DefaultTaskRouter(config, pool)
worker = WorkerService(config, pool, router)
await worker.start()
```

The poller manages the semaphore around this callback -- it acquires a slot before invoking the callback and releases it when the callback returns (or raises). The `TaskRouter` decides which `TaskExecutor` handles each claimed task; `DefaultTaskRouter` always routes to `GraphExecutor`.

### `HeartbeatHandle.cancel_event`

An `asyncio.Event` that the heartbeat manager sets when the heartbeat UPDATE returns 0 rows (meaning the lease was revoked -- e.g., by the reaper after expiry, or by a cancel API call). The executor checks this event between LangGraph super-steps and stops execution if set.

The handle also exposes `handle.lease_revoked` (bool) for a simple flag check.

## Co-located MCP Server (Task 5)

The worker service now ships with an in-process FastMCP server for the Phase 1 read-only tool set:

- `web_search(query: str, max_results: int = 5)`
- `read_url(url: str, max_chars: int = 5000)`
- `calculator(expression: str)`

Create the server from Python:

```python
from tools.server import create_mcp_server

server = create_mcp_server()
```

Run it locally over attachable HTTP:

```bash
cd services/worker-service
.venv/bin/python -m tools.server --transport http --host 127.0.0.1 --port 8000
```

Connect with the sample client:

```bash
cd services/worker-service
.venv/bin/python -m tools.sample_client --url http://127.0.0.1:8000/mcp
```

### Tool Contract

- `list_tools()` advertises exactly `web_search`, `read_url`, and `calculator`.
- `web_search` uses a provider abstraction with a Tavily-backed default implementation.
- `read_url` only fetches public `http`/`https` URLs, rejects private or loopback targets, limits redirects and response size, and returns sanitized readable text.
- `calculator` evaluates bounded arithmetic expressions without using `eval()`.
- local HTTP transport is available for manual MCP client attachment at `/mcp`.

### Runtime Configuration

The default search backend uses these environment variables:

| Variable | Required | Description |
|----------|----------|-------------|
| `TAVILY_API_KEY` | Yes for live `web_search` calls | Tavily API key used by the default search provider |

The tools package will auto-load local `.env` files from the current working directory, `src/worker-service/tools/.env`, and `src/worker-service/.env` without overriding an already-set process environment variable.

Unit tests do not require external API keys because they inject fake providers and mocked HTTP transports.

### Logging

The tools package logs startup and tool-call events to `stderr`. In stdio mode, `stdout` remains reserved for MCP protocol traffic.

## Running Tests

**Pytest Suite (No DB Required):**

Install dev dependencies and run pytest:

```bash
cd src/worker-service
pip install -e ".[dev]"
pytest tests/ -v
```

The pytest suite mixes unit tests with local MCP transport integration tests. No running PostgreSQL instance is required. The test suite covers:

- Backoff schedule progression and cap (`test_backoff.py`)
- Semaphore bounding of concurrent tasks (`test_semaphore.py`)
- Heartbeat interval timing and lease revocation signaling (`test_heartbeat.py`)
- Reaper jitter range and scan logic (`test_reaper.py`)
- Poller claim flow, LISTEN/NOTIFY filtering, backoff reset (`test_poller.py`)
- SQL query contract tests against the design doc (`test_queries.py`)
- Metrics collector counters/gauges and event constants (`test_metrics.py`)
- WorkerConfig defaults, uniqueness, immutability (`test_config.py`)
- FastMCP server registration and stable tool schemas (`test_mcp_server.py`)
- HTTP transport subprocess integration (`test_mcp_http_integration.py`)
- stdio transport subprocess integration (`test_mcp_stdio_integration.py`)
- local `.env` loading precedence for tool configuration (`test_env_loading.py`)
- Safe arithmetic parsing and rejection of unsafe expressions (`test_calculator_tool.py`)
- Search-tool provider normalization and transport error propagation (`test_web_search_tool.py`)
- URL reader bounds, SSRF-style rejection, and extraction behavior (`test_read_url_tool.py`)
- GraphExecutor completion, timeout, retry, dead-letter, and cancellation paths (`test_executor.py`)
- Lease-aware checkpointer lease validation, writes, and tuple reconstruction (`test_checkpointer.py`)

**Integration Tests (Real DB):**

A script `worker_integration_test.py` is included to test worker core primitives (Poller, Reaper, Heartbeat) using a real PostgreSQL database.

```bash
# Wait for the DB to be initialized, then run:
cd src/worker-service
# install dependencies using uv:
uv pip install psycopg2-binary
python worker_integration_test.py
```
