# Worker Service

The worker service now includes the foundational asyncio primitives for claiming, leasing, and recycling tasks in the Persistent Agent Runtime plus the Phase 1 co-located MCP server. It implements the database-as-queue pattern from the Phase 1 design using PostgreSQL `FOR UPDATE SKIP LOCKED`, with lease-based ownership to guarantee that no two workers execute the same task simultaneously.

The worker core remains intentionally free of LangGraph orchestration logic. It exports reusable primitives that Task 6 (Graph Executor) consumes, while `tools/` exposes the read-only FastMCP server contract that Task 6 will dispatch through.

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
 |
 |-- HeartbeatManager
 |     - One asyncio task per active task, every 15s
 |     - Extends lease_expiry by 60s
 |     - Detects lease revocation (UPDATE returns 0 rows)
 |     - Sets HeartbeatHandle.cancel_event so the executor can stop
 |
 |-- ReaperTask
       - Runs on every worker instance (not a singleton)
       - Jittered interval: 30s +/- 10s
       - Expired leases: requeue (retry_count < max_retries) or dead-letter
       - Task timeouts: dead-letter with reason 'task_timeout'
       - Emits pg_notify('new_task', pool_id) in the same transaction
```

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

## Integration Point (Task 6 -- Graph Executor)

The Graph Executor plugs in through two mechanisms:

### 1. `on_task_claimed` callback

Pass an async callback to `WorkerService` (or directly to `TaskPoller`). It receives the full claimed task row as a `dict[str, Any]` and is responsible for the entire execution lifecycle:

```python
async def execute_task(task_data: dict[str, Any]) -> None:
    task_id = str(task_data["task_id"])
    tenant_id = task_data["tenant_id"]

    # 1. Start heartbeat
    handle = worker.heartbeat.start_heartbeat(task_id, tenant_id)

    try:
        # 2. Build and run the LangGraph graph
        #    Check handle.cancel_event between super-steps
        async for event in graph.astream(...):
            if handle.cancel_event.is_set():
                break  # Lease was revoked
            # process event...

        # 3. Mark task completed (if not revoked)
        if not handle.lease_revoked:
            # UPDATE tasks SET status = 'completed' ...
            pass
    finally:
        # 4. Stop heartbeat
        await worker.heartbeat.stop_heartbeat(task_id)

worker = WorkerService(config, on_task_claimed=execute_task)
```

The poller manages the semaphore around this callback -- it acquires a slot before invoking the callback and releases it when the callback returns (or raises).

### 2. `HeartbeatHandle.cancel_event`

An `asyncio.Event` that the heartbeat manager sets when the heartbeat UPDATE returns 0 rows (meaning the lease was revoked -- e.g., by the reaper after expiry, or by a cancel API call). The executor should check this event between LangGraph super-steps and stop execution if set.

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

**Integration Tests (Real DB):**

A script `worker_integration_test.py` is included to test worker core primitives (Poller, Reaper, Heartbeat) using a real PostgreSQL database.

```bash
# Wait for the DB to be initialized, then run:
cd src/worker-service
# install dependencies using uv:
uv pip install psycopg2-binary
python worker_integration_test.py
```
