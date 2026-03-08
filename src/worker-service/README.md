# Worker Service Core

The worker service core provides the foundational asyncio primitives for claiming, leasing, and recycling tasks in the Persistent Agent Runtime. It implements the database-as-queue pattern from the Phase 1 design using PostgreSQL `FOR UPDATE SKIP LOCKED`, with lease-based ownership to guarantee that no two workers execute the same task simultaneously.

This module is intentionally free of LangGraph or graph execution logic. It exports reusable primitives that Task 6 (Graph Executor) consumes.

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

## Running Tests

Install dev dependencies and run pytest:

```bash
cd src/worker-service
pip install -e ".[dev]"
pytest tests/ -v
```

All tests are pure unit tests using mocked asyncpg connections. No running PostgreSQL instance is required. The test suite covers:

- Backoff schedule progression and cap (`test_backoff.py`)
- Semaphore bounding of concurrent tasks (`test_semaphore.py`)
- Heartbeat interval timing and lease revocation signaling (`test_heartbeat.py`)
- Reaper jitter range and scan logic (`test_reaper.py`)
- Poller claim flow, LISTEN/NOTIFY filtering, backoff reset (`test_poller.py`)
- SQL query contract tests against the design doc (`test_queries.py`)
- Metrics collector counters/gauges and event constants (`test_metrics.py`)
- WorkerConfig defaults, uniqueness, immutability (`test_config.py`)
