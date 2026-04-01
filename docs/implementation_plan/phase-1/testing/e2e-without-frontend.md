# End-to-End Testing Plan

## Goal

Validate the full request lifecycle across all three runtime components — **API Service (Java)**, **Worker Service (Python)**, and **PostgreSQL** — running as real processes against a shared database. Unlike existing integration tests that mock the LLM or manipulate DB state directly, these E2E tests submit tasks through the HTTP API and observe real worker execution with a mock LLM layer.

---

## 0. Context & Reference Materials

**Read these documents before implementing.** They contain the authoritative contracts for API, schema, state machine, and error handling behavior.

### 0.1 Design & Architecture Docs

| Document | Path | What it covers |
|----------|------|----------------|
| **Phase 1 Design (PRIMARY)** | `docs/design/phase-1/PHASE1_DURABLE_EXECUTION.md` | Canonical spec: entity model (§2), full API contract with request/response shapes (§3), state machine & transition table (§4), sequence diagrams for all 5 failure scenarios (§4), lease protocol & LISTEN/NOTIFY (§5.3), LangGraph execution & checkpointing (§5.4), error classification table (§5.5), all key SQL queries (§6.1), idempotency & crash recovery protocol (§6.2), security model (§6.3), observability (§6.4), demo scenario (§7) |
| Project Overview | `docs/PROJECT.md` | High-level vision, user stories, phases, tech stack |
| Phase 2 Design | `docs/design/phase-2/PHASE2_MULTI_AGENT.md` | Out of scope for E2E tests but useful for understanding forward-compatible fields (`tenant_id`, `worker_pool_id`) |
| Implementation Plan | `docs/implementation_plan/phase-1/plan.md` | Orchestrator plan, dependency graph between tasks 1-7, handoff outputs |
| CLAUDE.md | `CLAUDE.md` | Project-wide context: key architecture decisions, tech stack, project stages |

### 0.2 Database Schema

| File | Purpose |
|------|---------|
| `infrastructure/database/migrations/0001_phase1_durable_execution.sql` | **The schema DDL.** Defines `tasks`, `checkpoints`, `checkpoint_writes` tables, all indexes, and CHECK constraints. This is the source of truth for column names, types, defaults, and valid enum values. |
| `make db-reset-verify` | Make target that spins up a Docker PG container, applies the migration, and runs verification queries. Container is kept running natively. |
| `infrastructure/database/tests/verification.sql` | SQL verification queries for schema contract. |
| `infrastructure/database/README.md` | Schema documentation and usage instructions. |

### 0.3 API Service (Java / Spring Boot)

| File | Purpose |
|------|---------|
| `services/api-service/src/main/java/com/persistentagent/api/controller/TaskController.java` | **All REST endpoint handlers**: POST /tasks, GET /tasks/{id}, GET /tasks/{id}/checkpoints, POST /tasks/{id}/cancel, POST /tasks/{id}/redrive, GET /tasks/dead-letter |
| `services/api-service/src/main/java/com/persistentagent/api/controller/HealthController.java` | GET /health endpoint |
| `services/api-service/src/main/java/com/persistentagent/api/service/TaskService.java` | Business logic: validation, DB queries, state transitions |
| `services/api-service/src/main/java/com/persistentagent/api/repository/TaskRepository.java` | Raw JDBC queries against `tasks` and `checkpoints` tables |
| `services/api-service/src/main/java/com/persistentagent/api/config/ValidationConstants.java` | **Validation limits**: max sizes, allowed models list, allowed tools list, range constraints |
| `services/api-service/src/main/java/com/persistentagent/api/model/request/TaskSubmissionRequest.java` | Request DTO with field names and types |
| `services/api-service/src/main/java/com/persistentagent/api/model/request/AgentConfigRequest.java` | Nested agent_config DTO |
| `services/api-service/src/main/java/com/persistentagent/api/model/response/TaskStatusResponse.java` | Task status response shape |
| `services/api-service/src/main/java/com/persistentagent/api/model/response/CheckpointResponse.java` | Individual checkpoint response shape |
| `services/api-service/src/main/java/com/persistentagent/api/model/response/DeadLetterItemResponse.java` | Dead letter list item shape |
| `services/api-service/src/main/java/com/persistentagent/api/exception/GlobalExceptionHandler.java` | Maps exceptions to HTTP status codes (400, 404, 409, 500) |
| `services/api-service/src/main/resources/application.yml` | Spring config: DB connection (env vars `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD`), server port (`SERVER_PORT`, default 8080), HikariCP pool settings |
| `services/api-service/build.gradle` | Java 21, Spring Boot 3.4.3, PostgreSQL JDBC driver |
| `services/api-service/api_integration_test.py` | **Existing integration test** (359 lines, Python). Tests API endpoints with direct DB manipulation. Good reference for HTTP client patterns and assertion style. |

**API Service tests (Java):**
| File | What it tests |
|------|---------------|
| `services/api-service/src/test/java/.../TaskControllerTest.java` | Unit tests for controller endpoints |
| `services/api-service/src/test/java/.../TaskControllerIntegrationTest.java` | Integration tests (needs Docker PG, `INTEGRATION_TESTS_ENABLED=true`) |
| `services/api-service/src/test/java/.../TaskServiceTest.java` | Service layer unit tests |
| `services/api-service/src/test/java/.../HealthControllerTest.java` | Health endpoint tests |

### 0.4 Worker Service (Python / asyncio)

**Core subsystems:**
| File | Purpose |
|------|---------|
| `services/worker-service/core/worker.py` | **WorkerService class** — top-level orchestrator that wires poller, heartbeat, reaper. Has `start()`, `stop()`, `run_until_shutdown()` methods. |
| `services/worker-service/core/config.py` | **WorkerConfig** frozen dataclass — all tunable parameters with defaults (worker_id, db_dsn, lease_duration, heartbeat_interval, reaper_interval, max_concurrent, poll backoff) |
| `services/worker-service/core/db.py` | `create_pool(dsn)` and `create_listen_connection(dsn)` — asyncpg connection helpers |
| `services/worker-service/core/poller.py` | **TaskPoller** — claims tasks with `FOR UPDATE SKIP LOCKED`, uses LISTEN/NOTIFY on `new_task` channel, exponential backoff on empty queue, bounded by `asyncio.Semaphore(max_concurrent_tasks)` |
| `services/worker-service/core/heartbeat.py` | **HeartbeatManager** — per-task asyncio background task, fires every `heartbeat_interval_seconds`, extends lease, detects revocation (0 rows), sets `cancel_event` |
| `services/worker-service/core/reaper.py` | **ReaperTask** — runs on all workers every `reaper_interval ± jitter`, scans for expired leases and task timeouts, uses `UPDATE ... RETURNING` to avoid TOCTOU |
| `services/worker-service/core/logging.py` | `MetricsCollector` (counters/gauges), `configure_logging()`, `get_logger()` |

**Executor:**
| File | Purpose |
|------|---------|
| `services/worker-service/executor/graph.py` | **GraphExecutor** — builds `StateGraph` from `agent_config`, initializes `PostgresDurableCheckpointer`, runs `graph.astream()`, handles cost tracking via `CostTrackingCallback`, classifies errors (`_is_retryable_error`), manages completion/retry/dead-letter transitions |
| `services/worker-service/executor/router.py` | **TaskRouter protocol** and **DefaultTaskRouter** — always routes to GraphExecutor in Phase 1 |

**Checkpointer:**
| File | Purpose |
|------|---------|
| `services/worker-service/checkpointer/postgres.py` | **PostgresDurableCheckpointer** — implements `BaseCheckpointSaver`. Key: `put()` validates `lease_owner = :worker_id AND status = 'running'` before INSERT. Raises `LeaseRevokedException` if lease revoked. Also implements `aget_tuple()`, `alist()`, `aput_writes()`. |

**MCP Tools:**
| File | Purpose |
|------|---------|
| `services/worker-service/tools/definitions.py` | Tool schemas: `WEB_SEARCH_TOOL`, `READ_URL_TOOL`, `CALCULATOR_TOOL`, Pydantic argument models (`WebSearchArguments`, `ReadUrlArguments`, `CalculatorArguments`), `create_default_dependencies()` |
| `services/worker-service/tools/calculator.py` | `evaluate_expression()` — AST-based safe math evaluator |
| `services/worker-service/tools/providers/search.py` | `TavilySearchProvider` — web search via Tavily API |
| `services/worker-service/tools/read_url.py` | URL fetcher with SSRF guards |
| `services/worker-service/tools/app.py` | FastMCP server registration |
| `services/worker-service/tools/server.py` | MCP server entry point (`python -m tools.server --transport http`) |

**Dependencies** (`services/worker-service/pyproject.toml`):
- Python >= 3.11
- `asyncpg>=0.29.0`, `langgraph==1.0.5`, `langgraph-checkpoint==3.0.1`
- `langchain-core>=0.3.0`, `langchain-anthropic>=0.3.0`, `langchain-aws>=0.2.0`
- `mcp==1.26.0`, `pydantic>=2.11.0`, `structlog>=24.1.0`
- Dev: `pytest>=8.0`, `pytest-asyncio>=0.23`
- pytest config: `asyncio_mode = "auto"`

### 0.5 Existing Worker Service Tests (Reference for Patterns)

These are the most important files to study for mocking patterns and test structure:

| File | What it demonstrates |
|------|---------------------|
| **`services/worker-service/tests/test_integration.py`** | **Best reference for E2E.** Three async integration tests that start a real `WorkerService` with a real DB, mock `ChatAnthropic`, submit tasks via direct DB INSERT, and verify completion. Shows: mock LLM setup, `WorkerService` lifecycle (`start()/stop()`), DB cleanup, tool call simulation with `ToolCall` objects, checkpoint verification. |
| **`services/worker-service/tests/test_executor.py`** | Unit tests for `GraphExecutor`. Shows mock patterns for: simple completion, timeout (`asyncio.sleep`), retryable error classification, non-retryable errors, cancellation via `cancel_event`, `LeaseRevokedException`. |
| **`services/worker-service/tests/test_checkpointer_integration.py`** | Real DB integration for checkpointer. Shows `PostgresDurableCheckpointer` usage, lease validation, checkpoint write/read cycle. |
| `services/worker-service/tests/test_checkpointer.py` | Unit tests for checkpointer with mocked DB. |
| `services/worker-service/tests/test_poller.py` | Poller claim flow, LISTEN/NOTIFY filtering, backoff reset. |
| `services/worker-service/tests/test_heartbeat.py` | Heartbeat interval timing and lease revocation detection. |
| `services/worker-service/tests/test_reaper.py` | Reaper jitter range and scan logic. |
| `services/worker-service/tests/test_queries.py` | SQL query contract verification against design doc. |
| `services/worker-service/tests/conftest.py` | Shared fixtures: `WorkerConfig` defaults, `MetricsCollector` |

### 0.6 Key Patterns from Existing Tests

**Starting a WorkerService in-process (from `test_integration.py`):**
```python
pool = await create_pool(DB_DSN)
config = WorkerConfig(worker_id="test-worker", db_dsn=DB_DSN, ...)
router = DefaultTaskRouter(config, pool)
worker = WorkerService(config, pool, router)

with patch("executor.graph.ChatAnthropic") as MockChat:
    MockChat.return_value = mock_llm
    await worker.start()
    # ... test logic ...
    await worker.stop()
    await pool.close()
```

**Mocking a tool-calling LLM (from `test_integration.py`):**
```python
from langchain_core.messages import AIMessage, ToolCall

# Step 1: LLM decides to call a tool
call_msg = AIMessage(content="", tool_calls=[
    ToolCall(name="calculator", args={"expression": "5 * 5"}, id="call_123")
])
# Step 2: After seeing tool result, LLM gives final answer
final_msg = AIMessage(content="The result is 25!")

mock_llm = MagicMock()
mock_llm.ainvoke = AsyncMock(side_effect=[call_msg, final_msg])
mock_llm.bind_tools.return_value = mock_llm
```

**Inserting a test task directly into DB (from `test_integration.py`):**
```python
task_id = str(uuid.uuid4())
agent_config = {"system_prompt": "...", "model": "claude-3-5-sonnet-latest",
                "temperature": 0.5, "allowed_tools": ["calculator"]}
await conn.execute("""
    INSERT INTO tasks (task_id, tenant_id, agent_id, agent_config_snapshot,
                       status, input, max_retries, max_steps, task_timeout_seconds)
    VALUES ($1, 'default', 'test_agent', $2, 'queued', 'Test input', 3, 5, 300)
""", task_id, json.dumps(agent_config))
await conn.execute("SELECT pg_notify('new_task', 'shared')")
```

**DB cleanup between tests (from `test_integration.py`):**
```python
async with pool.acquire() as conn:
    await conn.execute("DELETE FROM checkpoint_writes")
    await conn.execute("DELETE FROM checkpoints")
    await conn.execute("DELETE FROM tasks")
```

### 0.7 Important Implementation Notes

1. **No `__main__` entrypoint exists** for the worker service. Tests must start `WorkerService` in-process (preferred) or create a `run_worker.py` script. The in-process approach is already validated in `test_integration.py`.

2. **The mock target is `executor.graph.ChatAnthropic`** — this patches the import in the graph executor module. `ChatBedrock` is used for non-Claude models but E2E tests should use Claude model names to hit the `ChatAnthropic` path.

3. **The worker's Python path** must include `services/worker-service/` as a source root. Imports are relative to that directory (e.g., `from core.config import WorkerConfig`, `from executor.graph import GraphExecutor`).

4. **The API service is a separate Java process** running on port 8080. E2E tests communicate with it via HTTP only — no in-process access to Java code.

5. **DB cleanup order matters** due to foreign keys: `checkpoint_writes` -> `checkpoints` -> `tasks`.

6. **The `executor` package** is not in `pyproject.toml`'s `[tool.setuptools.packages.find] include` list (it only lists `core*`, `checkpointer*`, `tools*`). This may need to be added, or tests must use `sys.path` manipulation. Check existing test imports — `test_integration.py` imports `from executor.graph import GraphExecutor` successfully, suggesting the package structure works in the test runner context.

7. **Cost tracking** uses `CostTrackingCallback` on `on_llm_end`. With mocked LLMs, `llm_output` / `token_usage` won't be populated unless explicitly set on the mock response. Tests verifying cost > 0 need to ensure the mock LLM returns proper `llm_output`.

---

## 1. Infrastructure Setup

### 1.1 PostgreSQL (Docker)

```bash
docker run -d \
  --name par-e2e-postgres \
  -e POSTGRES_USER=postgres \
  -e POSTGRES_PASSWORD=postgres \
  -e POSTGRES_DB=persistent_agent_runtime \
  -p 55432:5432 \
  postgres:16

# Wait for readiness
until docker exec par-e2e-postgres pg_isready -U postgres; do sleep 0.5; done

# Apply schema
psql postgresql://postgres:postgres@localhost:55432/persistent_agent_runtime \
  < infrastructure/database/migrations/0001_phase1_durable_execution.sql
```

### 1.2 API Service (Java Spring Boot)

```bash
cd services/api-service
DB_HOST=localhost DB_PORT=55432 DB_NAME=persistent_agent_runtime \
  DB_USER=postgres DB_PASSWORD=postgres SERVER_PORT=8080 \
  ./gradlew bootRun
```

Readiness check: `curl -sf http://localhost:8080/v1/health`

### 1.3 Worker Service (Python)

The worker has no standalone `__main__` entrypoint today. The E2E harness must launch `WorkerService` programmatically:

```python
import asyncio
from core.config import WorkerConfig
from core.db import create_pool
from core.worker import WorkerService
from executor.router import DefaultTaskRouter

async def run_worker():
    config = WorkerConfig(
        db_dsn="postgresql://postgres:postgres@localhost:55432/persistent_agent_runtime",
        heartbeat_interval_seconds=2,
        lease_duration_seconds=10,
        reaper_interval_seconds=5,
        reaper_jitter_seconds=1,
    )
    pool = await create_pool(config.db_dsn)
    router = DefaultTaskRouter(config, pool)
    worker = WorkerService(config, pool, router)
    await worker.run_until_shutdown()

asyncio.run(run_worker())
```

> **Prerequisite**: Create `services/worker-service/__main__.py` or `services/worker-service/run_worker.py` so the worker can be launched as a subprocess from the test harness.

### 1.4 Test Runner

Python pytest with `pytest-asyncio`. The test process:
1. Starts PostgreSQL container (or assumes it is running).
2. Applies schema migration.
3. Starts API service as a subprocess (gradle bootRun) — waits for `/v1/health`.
4. Starts worker service as a subprocess — with `ChatAnthropic` patched via env-controlled mock mode (see Section 2).
5. Runs test scenarios.
6. Tears down subprocesses.

---

## 2. LLM Mock Strategy

Real LLM calls are non-deterministic, slow, and cost money. The E2E tests need a **deterministic mock** that still exercises the full LangGraph graph execution, checkpoint writes, and tool dispatches.

### Option A: In-Process Mock (Recommended for Phase 1)

Patch `executor.graph.ChatAnthropic` at the module level before importing `DefaultTaskRouter`. The worker is started **in-process** as an `asyncio.Task` (similar to `test_integration.py`), giving full control over the mock. This is the pattern already validated in the existing integration tests.

```python
from unittest.mock import patch, MagicMock, AsyncMock
from langchain_core.messages import AIMessage, ToolCall

# Scenario: LLM calls calculator, then returns final answer
call_msg = AIMessage(content="", tool_calls=[
    ToolCall(name="calculator", args={"expression": "2 + 2"}, id="call_1")
])
final_msg = AIMessage(content="The answer is 4.")

mock_llm = MagicMock()
mock_llm.ainvoke = AsyncMock(side_effect=[call_msg, final_msg])
mock_llm.bind_tools.return_value = mock_llm

with patch("executor.graph.ChatAnthropic") as MockChat:
    MockChat.return_value = mock_llm
    # ... start worker, submit task via HTTP, observe completion
```

### Option B: Env-Controlled Mock Mode (Future)

Add a `MOCK_LLM=true` env var to the worker that swaps `ChatAnthropic` for a `FakeChatModel` at graph build time. This enables running the worker as a true subprocess.

### Mock Response Catalog

| Scenario | LLM Responses | Tools Used |
|----------|---------------|------------|
| Simple completion | `AIMessage("Hello!")` | None |
| Calculator tool call | `AIMessage(tool_calls=[calculator("5*5")])` -> `AIMessage("25")` | calculator |
| Multi-tool chain | `AIMessage(tool_calls=[web_search("test")])` -> `AIMessage(tool_calls=[calculator("1+1")])` -> `AIMessage("Done")` | web_search, calculator |
| Retryable error | `raise Exception("503 Service Unavailable")` on 1st call, then `AIMessage("ok")` | None |
| Non-retryable error | `raise Exception("400 Bad Request: invalid model")` | None |
| Slow execution (timeout) | `await asyncio.sleep(forever)` | None |

---

## 3. Test Scenarios

### 3.1 Happy Path: Submit -> Execute -> Complete

**What it validates:** Full lifecycle — API submission, DB queue insert, worker claim via `FOR UPDATE SKIP LOCKED`, LangGraph execution, checkpointer writes, task completion.

**Steps:**
1. `POST /v1/tasks` with `agent_id=e2e_agent`, `model=claude-sonnet-4-6`, `allowed_tools=["calculator"]`, `input="What is 5*5?"`.
2. Assert response `201` with `status=queued`.
3. Poll `GET /v1/tasks/{id}` until `status=completed` (timeout: 15s).
4. Assert `output` contains `"25"`.
5. `GET /v1/tasks/{id}/checkpoints` — assert `len(checkpoints) >= 2` (agent node + tools node).
6. Assert each checkpoint has `worker_id` set, `step_number` sequential.
7. Assert `total_cost_microdollars >= 0` on the task response.

**Mock LLM:** Calculator tool call -> final answer.

**Key assertions:**
- Status transitions: `queued -> running -> completed`
- `lease_owner` is set while running, `NULL` after completion
- `version` incremented (at least once for claim, once for completion)

---

### 3.2 Simple Completion (No Tools)

**What it validates:** Graph execution without tool nodes — the `agent -> END` path.

**Steps:**
1. `POST /v1/tasks` with `allowed_tools=[]`, `input="Say hello"`.
2. Poll until `completed`.
3. Assert `output.result` = mock response content.
4. Checkpoints: at least 1 (agent node).

**Mock LLM:** `AIMessage("Hello there!")`

---

### 3.3 Task Cancellation While Queued

**What it validates:** API cancellation path for a task that hasn't been claimed yet.

**Steps:**
1. Pause the worker (don't start it, or use a semaphore of 0).
2. `POST /v1/tasks` — task stays `queued`.
3. `POST /v1/tasks/{id}/cancel`.
4. Assert `status=dead_letter`, `dead_letter_reason=cancelled_by_user`.
5. Verify `lease_owner=NULL`.

---

### 3.4 Task Cancellation While Running

**What it validates:** Cancellation of an actively executing task — verifies the API clears the lease and the worker's heartbeat detects revocation.

**Steps:**
1. Mock LLM with a slow response (`asyncio.sleep(30)` before returning).
2. Submit task, poll until `status=running`.
3. `POST /v1/tasks/{id}/cancel`.
4. Assert task transitions to `dead_letter` with `cancelled_by_user`.
5. Verify worker logs `LEASE_REVOKED` or `cancelled` (heartbeat detects 0 rows).
6. Verify no further checkpoint writes after cancellation.

**DB verification:**
```sql
SELECT lease_owner, lease_expiry, status, dead_letter_reason
FROM tasks WHERE task_id = :id;
-- Expected: lease_owner=NULL, status='dead_letter', dead_letter_reason='cancelled_by_user'
```

---

### 3.5 Worker Crash & Lease Expiry Recovery

**What it validates:** Reaper detects expired leases and re-queues the task. A second execution attempt succeeds.

**Steps:**
1. Submit task.
2. Poll until `status=running`, note `lease_owner`.
3. Directly update DB to simulate crash: set `lease_expiry = NOW() - INTERVAL '1 second'` (simulating the worker dying without releasing the lease).
4. Wait for reaper cycle (~5-7s with test config).
5. Assert task is re-queued: `status=queued`, `retry_count=1`, `lease_owner=NULL`.
6. Let worker reclaim and complete.
7. Assert final `status=completed`.

**Alternative (process-level):** Kill the worker process, start a new one, verify recovery.

---

### 3.6 Retryable Error with Exponential Backoff

**What it validates:** Error classification, retry_count increment, backoff calculation, re-execution.

**Steps:**
1. Mock LLM: 1st call raises `Exception("503 Service Unavailable")`, 2nd call returns `AIMessage("recovered")`.
2. Submit task with `max_retries=3`.
3. Wait for 1st execution attempt to fail.
4. Verify via DB: `status=queued`, `retry_count=1`, `retry_after > NOW()`, `last_error_code=retryable_error`.
5. After `retry_after` passes, verify worker reclaims.
6. Assert final `status=completed`.

**DB verification:**
```sql
SELECT retry_count, retry_after, last_error_code, last_error_message, status
FROM tasks WHERE task_id = :id;
```

---

### 3.7 Retries Exhausted -> Dead Letter

**What it validates:** Task dead-letters after `max_retries` retryable failures.

**Steps:**
1. Mock LLM: always raises `Exception("503 Service Unavailable")`.
2. Submit task with `max_retries=1`.
3. Wait for both attempts to fail.
4. Assert `status=dead_letter`, `dead_letter_reason=retries_exhausted`, `retry_count=1`.

---

### 3.8 Non-Retryable Error -> Immediate Dead Letter

**What it validates:** 4xx errors skip retry and go directly to dead letter.

**Steps:**
1. Mock LLM: raises `Exception("400 Bad Request: invalid prompt")`.
2. Submit task.
3. Assert task reaches `dead_letter` without incrementing `retry_count`.
4. `dead_letter_reason=non_retryable_error`.

---

### 3.9 Task Timeout

**What it validates:** Executor's `asyncio.wait_for` timeout and dead-letter transition.

**Steps:**
1. Mock LLM: `await asyncio.sleep(999)` (never returns).
2. Submit task with `task_timeout_seconds=3`.
3. Assert task reaches `dead_letter`, `dead_letter_reason=task_timeout`.

**Note:** Separately, the reaper also enforces timeout based on `created_at + task_timeout_seconds < NOW()`. This covers the case where the executor timeout doesn't fire (e.g., worker crash during a long task).

---

### 3.10 Max Steps Exceeded

**What it validates:** LangGraph `GraphRecursionError` triggers dead letter.

**Steps:**
1. Mock LLM: always returns a tool call (infinite loop of `calculator("1+1")`).
2. Submit task with `max_steps=3`, `allowed_tools=["calculator"]`.
3. Assert task reaches `dead_letter`, `dead_letter_reason=max_steps_exceeded`.

---

### 3.11 Redrive from Dead Letter

**What it validates:** Re-queuing a dead-lettered task and resuming from last checkpoint.

**Steps:**
1. Execute scenario 3.8 (non-retryable error) to get a dead-lettered task.
2. `POST /v1/tasks/{id}/redrive`.
3. Assert `status=queued`, `retry_count=0`, `dead_letter_reason=NULL`.
4. Change mock to return success on next attempt.
5. Wait for completion.
6. Assert `status=completed`.
7. Verify checkpoints: the previously saved checkpoints still exist; new checkpoints are appended.

---

### 3.12 Dead Letter Queue Listing

**What it validates:** API filtering and pagination for dead-lettered tasks.

**Steps:**
1. Create 5 dead-lettered tasks across 2 different `agent_id` values.
2. `GET /v1/tasks/dead-letter?agent_id=agent_A` — assert only agent_A tasks returned.
3. `GET /v1/tasks/dead-letter?limit=2` — assert exactly 2 items.
4. Assert items ordered by `dead_lettered_at DESC`.

---

### 3.13 Checkpoint History Verification

**What it validates:** Checkpoint ordering, step numbers, metadata, cost accumulation.

**Steps:**
1. Run a multi-step task (tool call + final answer).
2. `GET /v1/tasks/{id}/checkpoints`.
3. Assert `step_number` values are sequential (1, 2, ...).
4. Assert `node_name` extracted from `metadata_payload.source` (`"agent"`, `"tools"`, etc.).
5. Assert `worker_id` matches the worker that executed.
6. If cost tracking is active, verify `sum(cost_microdollars)` across checkpoints matches `total_cost_microdollars` on task status.

---

### 3.14 Health Endpoint

**What it validates:** Service liveness and DB connectivity reporting.

**Steps:**
1. `GET /v1/health`.
2. Assert `status=healthy`, `database=connected`.
3. Assert `queued_tasks` and `active_workers` are integers >= 0.

---

### 3.15 Input Validation (API-only, no worker needed)

**What it validates:** Rejection of malformed submissions at the API boundary.

| Test Case | Payload | Expected |
|-----------|---------|----------|
| Invalid model | `model: "gpt-5-ultra"` | 400 |
| Invalid tool | `allowed_tools: ["rm_rf"]` | 400 |
| Missing agent_id | omit `agent_id` | 400 |
| Input too large | `input: "x" * 102400` | 400 |
| Temperature out of range | `temperature: 3.0` | 400 |
| Timeout out of range | `task_timeout_seconds: 100000` | 400 |
| Max steps out of range | `max_steps: 0` | 400 |
| Missing system_prompt | omit `system_prompt` | 400 |
| Task not found | `GET /v1/tasks/{random-uuid}` | 404 |
| Cancel completed task | cancel a `completed` task | 409 |
| Redrive non-dead-letter | redrive a `queued` task | 409 |

---

### 3.16 Concurrent Task Execution

**What it validates:** Worker concurrency, `FOR UPDATE SKIP LOCKED` preventing double-claims, semaphore bounding.

**Steps:**
1. Submit 5 tasks simultaneously.
2. Verify all 5 are claimed (each has distinct `lease_owner` timestamp, same worker_id).
3. Verify all 5 complete.
4. Verify no task was executed twice (check checkpoint worker_id consistency per task).

---

### 3.17 Multi-Worker Coordination

**What it validates:** Two workers don't claim the same task; tasks are distributed.

**Steps:**
1. Start 2 worker instances (different `worker_id`).
2. Submit 6 tasks.
3. Verify tasks are distributed across both workers (inspect `lease_owner` in DB).
4. Verify no overlapping ownership for any single task.
5. All tasks complete exactly once.

---

### 3.18 LISTEN/NOTIFY Fast Path

**What it validates:** Worker picks up new tasks immediately via PostgreSQL notification, not just polling.

**Steps:**
1. Start worker (idle, no pending tasks).
2. Submit a task.
3. Measure time from submission to `status=running`.
4. Assert claim latency < 1s (LISTEN/NOTIFY should wake the poller immediately).

---

### 3.19 Crash Recovery: Node Re-Execution & Checkpoint Resume (Design Doc D1, D2, D4, S1)

**What it validates:** After a worker crash, the replacement worker resumes from the last checkpoint. Previously completed nodes are NOT re-executed. The interrupted in-flight node IS re-executed. Checkpoint history shows the crash boundary via different `worker_id` values.

**Steps:**
1. Mock LLM: 1st call returns tool call (calculator), 2nd call raises `Exception("503")` to simulate crash-like failure on the second agent turn.
2. Submit task with `max_retries=3`, `allowed_tools=["calculator"]`.
3. Wait for task to be re-queued (retryable error).
4. Change mock to succeed on the next attempt (returns `AIMessage("done")`).
5. Wait for task to complete.
6. `GET /v1/tasks/{id}/checkpoints` — assert:
   - Checkpoints from the first attempt exist (written by worker before failure).
   - New checkpoints from the second attempt are appended.
   - `worker_id` on pre-crash checkpoints differs from (or equals) post-crash checkpoints — the crash boundary is visible.
   - Previously completed nodes (calculator tool result) are NOT duplicated.

**Key assertions:**
- Checkpoint count after recovery > checkpoint count before crash.
- No duplicate checkpoint entries for the same super-step.
- Two different `worker_id` values appear in checkpoints if using two worker instances.

---

### 3.20 Crash Between Last Checkpoint and Task Completion (Design Doc §6.1)

**What it validates:** If a worker crashes after the final LangGraph node completes but before the `UPDATE tasks SET status='completed'` is written, the task is recovered. On resume, LangGraph's `astream()` yields nothing (graph already at end state) and the worker marks it completed.

**Steps:**
1. Mock LLM: returns `AIMessage("done")` (single-step, no tools).
2. Patch the executor to crash (raise exception) AFTER `astream()` finishes but BEFORE the completion `UPDATE`.
3. Wait for reaper to reclaim (task still in `running` with expired lease).
4. On retry, the worker initializes LangGraph, loads the existing checkpoint, `astream()` yields 0 events.
5. Assert task reaches `completed`.
6. Assert no new checkpoints are written on the recovery pass.

---

### 3.21 Reaper: Expired Lease with Retries Exhausted -> Dead Letter (Design Doc §6.1)

**What it validates:** When the reaper finds a task with an expired lease AND `retry_count >= max_retries`, it dead-letters directly instead of re-queuing.

**Steps:**
1. Submit task with `max_retries=0`.
2. Inject DB state: `status=running`, `lease_expiry=NOW()-1min`, `retry_count=0`, `lease_owner='crashed-worker'`.
3. Wait for reaper cycle.
4. Assert `status=dead_letter`, `dead_letter_reason=retries_exhausted`.
5. Assert `last_worker_id='crashed-worker'`, `last_error_code='retries_exhausted'`.

---

### 3.22 Retry Backoff Invisibility Window (Design Doc R3, §5.5)

**What it validates:** A re-queued task with `retry_after` in the future is invisible to workers until the backoff expires. The claim query enforces `retry_after IS NULL OR retry_after < NOW()`.

**Steps:**
1. Submit task.
2. Inject DB state: `status=queued`, `retry_after=NOW()+30s`, `retry_count=1`.
3. Start worker, wait 3s.
4. Assert task is still `queued` (not claimed) — `lease_owner IS NULL`.
5. Update `retry_after=NOW()-1s`.
6. Wait for worker to claim (poll + NOTIFY).
7. Assert task transitions to `running`.

---

### 3.23 Version Field Increments on Transitions (Design Doc §2)

**What it validates:** The `version` column increments on every lifecycle transition (claim, retry, completion, dead-letter, cancel, redrive) — used for auditing/ETags.

**Steps:**
1. Submit task, record `version` (should be 1).
2. Wait for `status=running`, check `version` incremented (claim: version=2).
3. Wait for `status=completed`, check `version` incremented again (completion: version=3).
4. For a second task: submit -> cancel -> assert version incremented.
5. Redrive -> assert version incremented.

---

### 3.24 Error Fields Cleared on Completion (Design Doc §5.5)

**What it validates:** `last_error_code` and `last_error_message` are cleared when a task successfully completes after a prior retry failure.

**Steps:**
1. Mock LLM: 1st call raises retryable error, 2nd call succeeds.
2. Submit task with `max_retries=3`.
3. After 1st failure: assert `last_error_code='retryable_error'`, `last_error_message` is set.
4. After recovery and completion: assert `last_error_code=NULL`, `last_error_message=NULL`.

**Note:** This requires the executor to clear error fields on completion. Verify against the `UPDATE tasks SET status='completed'` query in the design doc (§6.1) which specifies `last_error_code = NULL, last_error_message = NULL`.

---

### 3.25 retry_history Append-Only (Design Doc §6.1)

**What it validates:** Each retry appends a timestamp to the `retry_history` JSONB array.

**Steps:**
1. Mock LLM: always raises `Exception("503")`.
2. Submit task with `max_retries=2`.
3. Wait for task to exhaust retries and dead-letter.
4. Query DB directly: `SELECT retry_history FROM tasks WHERE task_id = :id`.
5. Assert `retry_history` is an array with 2 entries (one per retry).
6. Assert entries are valid timestamps, ordered chronologically.

---

### 3.26 Zombie Checkpointer Protection / LeaseRevokedException (Design Doc §5.4, §6.2)

**What it validates:** If a worker's lease is revoked mid-execution, the checkpointer's `put()` detects the stale lease and raises `LeaseRevokedException`, preventing stale checkpoint writes.

**Steps:**
1. Mock LLM: 1st call returns tool call, introducing a delay before the 2nd call.
2. Submit task.
3. Wait for task to be `running` and first checkpoint to be written.
4. Directly revoke lease in DB: `UPDATE tasks SET lease_owner=NULL, status='dead_letter', dead_letter_reason='cancelled_by_user' WHERE task_id=:id`.
5. Assert worker detects revocation (via heartbeat returning 0 rows or checkpointer `put()` failing).
6. Assert no new checkpoints are written after revocation.
7. Assert no split-brain: task stays in `dead_letter`, no competing completion.

---

### 3.27 Tenant Scoping (Design Doc S7, §6.3)

**What it validates:** All queries are scoped by `tenant_id`. In Phase 1, `tenant_id='default'` is resolved internally.

**Steps:**
1. Submit task (tenant_id defaults to "default").
2. Directly insert a task in DB with `tenant_id='other_tenant'`, `status='dead_letter'`.
3. `GET /v1/tasks/dead-letter` — assert only `default` tenant tasks appear.
4. Attempt to get the `other_tenant` task via `GET /v1/tasks/{id}` — assert 404 (tenant-scoped lookup excludes it).

---

### 3.28 Multi-Reaper Coordination (Design Doc R6)

**What it validates:** Multiple worker instances running reaper logic don't conflict — `UPDATE ... RETURNING` ensures exactly one reaper reclaims each task.

**Steps:**
1. Start 2 worker instances (both run reaper loops).
2. Insert 5 tasks with expired leases directly into DB.
3. Wait for reaper cycles on both workers.
4. Assert all 5 tasks are reclaimed exactly once (check `retry_count=1` for each, not 2).
5. Assert no errors in either worker's logs about conflicting reaper claims.

---

## 4. Project Structure

```
tests/backend-integration/
  PLAN.md                    # This file
  conftest.py                # Shared fixtures: DB setup, API client, worker launcher
  helpers/
    api_client.py            # HTTP client wrapping /v1/* endpoints
    db.py                    # Direct DB access for assertions and state injection
    worker_launcher.py       # Starts WorkerService in-process with mock LLM
    mock_llm.py              # Mock LLM response builders
  test_happy_path.py         # 3.1, 3.2
  test_cancellation.py       # 3.3, 3.4
  test_recovery.py           # 3.5, 3.6, 3.7
  test_error_handling.py     # 3.8, 3.9, 3.10
  test_redrive.py            # 3.11, 3.12
  test_checkpoints.py        # 3.13 (checkpoint history, cost)
  test_health.py             # 3.14
  test_validation.py         # 3.15
  test_concurrency.py        # 3.16, 3.17, 3.18
  test_crash_resume.py       # 3.19, 3.20 (crash boundary, node re-execution, zero-step resume)
  test_reaper_edges.py       # 3.21, 3.22, 3.28 (expired+exhausted, backoff invisibility, multi-reaper)
  test_lifecycle_fields.py   # 3.23, 3.24, 3.25 (version increments, error field cleanup, retry_history)
  test_lease_safety.py       # 3.26 (zombie checkpointer, LeaseRevokedException)
  test_tenant_isolation.py   # 3.27 (tenant-scoped query isolation)
```

---

## 5. Shared Fixtures (`conftest.py`)

```python
import asyncio
import uuid
import pytest
import pytest_asyncio
import asyncpg
import subprocess
import time
import urllib.request

DB_DSN = "postgresql://postgres:postgres@localhost:55432/persistent_agent_runtime"
API_BASE = "http://localhost:8080/v1"

@pytest_asyncio.fixture
async def db_pool():
    """Provide a clean database for each test."""
    pool = await asyncpg.create_pool(DB_DSN, min_size=2, max_size=5)
    # Clean all tables
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM checkpoint_writes")
        await conn.execute("DELETE FROM checkpoints")
        await conn.execute("DELETE FROM tasks")
    yield pool
    await pool.close()

@pytest.fixture
def api_client():
    """HTTP client for the API service."""
    from helpers.api_client import ApiClient
    return ApiClient(API_BASE)

@pytest_asyncio.fixture
async def worker(db_pool):
    """Start an in-process worker with mock LLM and yield it."""
    from helpers.worker_launcher import create_worker
    worker_instance = await create_worker(db_pool)
    await worker_instance.start()
    yield worker_instance
    await worker_instance.stop()
```

---

## 6. Key Helper: API Client (`helpers/api_client.py`)

```python
import json
import urllib.request
import urllib.error
from typing import Optional

class ApiClient:
    def __init__(self, base_url: str):
        self.base = base_url

    def submit_task(self, **overrides) -> dict:
        payload = {
            "agent_id": overrides.get("agent_id", "e2e_agent"),
            "agent_config": {
                "system_prompt": overrides.get("system_prompt", "You are a test assistant."),
                "model": overrides.get("model", "claude-sonnet-4-6"),
                "temperature": overrides.get("temperature", 0.5),
                "allowed_tools": overrides.get("allowed_tools", ["calculator"]),
            },
            "input": overrides.get("input", "What is 2+2?"),
            "max_retries": overrides.get("max_retries", 3),
            "max_steps": overrides.get("max_steps", 10),
            "task_timeout_seconds": overrides.get("task_timeout_seconds", 60),
        }
        req = urllib.request.Request(
            f"{self.base}/tasks", method="POST",
            headers={"Content-Type": "application/json"},
            data=json.dumps(payload).encode(),
        )
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())

    def get_task(self, task_id: str) -> dict:
        with urllib.request.urlopen(f"{self.base}/tasks/{task_id}") as resp:
            return json.loads(resp.read())

    def get_checkpoints(self, task_id: str) -> dict:
        with urllib.request.urlopen(f"{self.base}/tasks/{task_id}/checkpoints") as resp:
            return json.loads(resp.read())

    def cancel_task(self, task_id: str) -> dict:
        req = urllib.request.Request(f"{self.base}/tasks/{task_id}/cancel", method="POST")
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())

    def redrive_task(self, task_id: str) -> dict:
        req = urllib.request.Request(f"{self.base}/tasks/{task_id}/redrive", method="POST")
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())

    def get_dead_letters(self, agent_id: Optional[str] = None, limit: int = 50) -> dict:
        url = f"{self.base}/tasks/dead-letter?limit={limit}"
        if agent_id:
            url += f"&agent_id={agent_id}"
        with urllib.request.urlopen(url) as resp:
            return json.loads(resp.read())

    def health(self) -> dict:
        with urllib.request.urlopen(f"{self.base}/health") as resp:
            return json.loads(resp.read())

    def poll_until(self, task_id: str, target_status: str, timeout: float = 15.0, interval: float = 0.5) -> dict:
        """Poll GET /tasks/{id} until status matches or timeout."""
        import time
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            task = self.get_task(task_id)
            if task["status"] == target_status:
                return task
            # Also return if terminal and not the target
            if task["status"] in ("completed", "dead_letter") and task["status"] != target_status:
                return task
            time.sleep(interval)
        raise TimeoutError(f"Task {task_id} did not reach {target_status} within {timeout}s. Last: {task['status']}")
```

---

## 7. Key Helper: Mock LLM Builder (`helpers/mock_llm.py`)

```python
from unittest.mock import MagicMock, AsyncMock
from langchain_core.messages import AIMessage, ToolCall

def simple_response(content: str = "Hello!"):
    """LLM returns a single message, no tool calls."""
    mock = MagicMock()
    mock.ainvoke = AsyncMock(return_value=AIMessage(content=content))
    mock.bind_tools.return_value = mock
    return mock

def calculator_tool_call(expression: str = "2 + 2", final_answer: str = "The answer is 4."):
    """LLM calls calculator, then returns final answer."""
    call_msg = AIMessage(content="", tool_calls=[
        ToolCall(name="calculator", args={"expression": expression}, id="call_1")
    ])
    final_msg = AIMessage(content=final_answer)
    mock = MagicMock()
    mock.ainvoke = AsyncMock(side_effect=[call_msg, final_msg])
    mock.bind_tools.return_value = mock
    return mock

def retryable_then_success(error_msg: str = "503 Service Unavailable", final: str = "recovered"):
    """First call raises retryable error, second succeeds."""
    mock = MagicMock()
    mock.ainvoke = AsyncMock(side_effect=[
        Exception(error_msg),
        AIMessage(content=final),
    ])
    mock.bind_tools.return_value = mock
    return mock

def always_fails(error_msg: str = "400 Bad Request: invalid"):
    """Every call raises a non-retryable error."""
    mock = MagicMock()
    mock.ainvoke = AsyncMock(side_effect=Exception(error_msg))
    mock.bind_tools.return_value = mock
    return mock

def slow_response(delay: float = 999):
    """LLM call blocks for a long time (for timeout tests)."""
    import asyncio
    async def slow(*args, **kwargs):
        await asyncio.sleep(delay)
        return AIMessage(content="too late")
    mock = MagicMock()
    mock.ainvoke = AsyncMock(side_effect=slow)
    mock.bind_tools.return_value = mock
    return mock

def infinite_tool_loop():
    """Always returns a calculator tool call (for max_steps tests)."""
    def make_call(*args, **kwargs):
        return AIMessage(content="", tool_calls=[
            ToolCall(name="calculator", args={"expression": "1+1"}, id=f"call_{id(args)}")
        ])
    mock = MagicMock()
    mock.ainvoke = AsyncMock(side_effect=make_call)
    mock.bind_tools.return_value = mock
    return mock
```

---

## 8. Key Helper: Worker Launcher (`helpers/worker_launcher.py`)

```python
from unittest.mock import patch, MagicMock
from core.config import WorkerConfig
from core.worker import WorkerService
from executor.router import DefaultTaskRouter

# Aggressive timing for fast test execution
DEFAULT_TEST_CONFIG = dict(
    heartbeat_interval_seconds=2,
    lease_duration_seconds=10,
    reaper_interval_seconds=5,
    reaper_jitter_seconds=1,
    max_concurrent_tasks=10,
    poll_backoff_initial_ms=50,
    poll_backoff_max_ms=500,
)

async def create_worker(pool, mock_llm=None, config_overrides=None, worker_id=None):
    """Create a WorkerService with an optional mock LLM.

    If mock_llm is provided, patches ChatAnthropic to return it.
    Returns the worker (caller must start/stop).
    """
    cfg = {**DEFAULT_TEST_CONFIG, **(config_overrides or {})}
    if worker_id:
        cfg["worker_id"] = worker_id

    config = WorkerConfig(
        db_dsn="postgresql://postgres:postgres@localhost:55432/persistent_agent_runtime",
        **cfg,
    )

    if mock_llm:
        patcher = patch("executor.graph.ChatAnthropic", return_value=mock_llm)
        patcher.start()
        # Store patcher on worker for cleanup

    router = DefaultTaskRouter(config, pool)
    worker = WorkerService(config, pool, router)

    if mock_llm:
        worker._llm_patcher = patcher  # Attach for cleanup

    return worker

async def stop_worker(worker):
    """Stop worker and clean up mock patches."""
    await worker.stop()
    if hasattr(worker, '_llm_patcher'):
        worker._llm_patcher.stop()
```

---

## 9. Technical Details Reference

### 9.1 Database Connection

| Component | DSN | Port |
|-----------|-----|------|
| PostgreSQL | `postgresql://postgres:postgres@localhost:55432/persistent_agent_runtime` | 55432 |
| API Service (JDBC) | `jdbc:postgresql://localhost:55432/persistent_agent_runtime` | 8080 |
| Worker Service (asyncpg) | same as PostgreSQL DSN | N/A |

### 9.2 Task Status Machine

```
queued ──claim──> running ──success──> completed
  ^                  │
  │          retryable error
  │          (retry_count < max_retries)
  └──────────────────┘

queued/running ──cancel──> dead_letter (cancelled_by_user)
running ──non-retryable──> dead_letter (non_retryable_error)
running ──timeout──> dead_letter (task_timeout)
running ──max_steps──> dead_letter (max_steps_exceeded)
running ──retries exhausted──> dead_letter (retries_exhausted)

dead_letter ──redrive──> queued
```

### 9.3 Claim Query (Worker)

```sql
UPDATE tasks
SET status = 'running',
    lease_owner = :worker_id,
    lease_expiry = NOW() + :lease_duration * INTERVAL '1 second',
    version = version + 1
WHERE task_id = (
    SELECT task_id FROM tasks
    WHERE status = 'queued'
      AND worker_pool_id = :pool_id
      AND (retry_after IS NULL OR retry_after <= NOW())
    ORDER BY created_at
    FOR UPDATE SKIP LOCKED
    LIMIT 1
)
RETURNING *;
```

### 9.4 Heartbeat Query

```sql
UPDATE tasks
SET lease_expiry = NOW() + :lease_duration * INTERVAL '1 second',
    updated_at = NOW()
WHERE task_id = :task_id
  AND lease_owner = :worker_id
  AND status = 'running';
-- Returns 0 rows if lease was revoked (cancellation or reaper)
```

### 9.5 Reaper Queries

**Expired leases:**
```sql
UPDATE tasks
SET status = 'queued',
    lease_owner = NULL,
    lease_expiry = NULL,
    retry_count = retry_count + 1,
    retry_after = NOW() + (2 ^ retry_count) * INTERVAL '1 second',
    version = version + 1,
    updated_at = NOW()
WHERE status = 'running'
  AND lease_expiry < NOW()
RETURNING task_id;
```

**Task timeout:**
```sql
UPDATE tasks
SET status = 'dead_letter',
    dead_letter_reason = 'task_timeout',
    dead_lettered_at = NOW(),
    lease_owner = NULL,
    lease_expiry = NULL,
    version = version + 1,
    updated_at = NOW()
WHERE status IN ('running', 'queued')
  AND created_at + task_timeout_seconds * INTERVAL '1 second' < NOW()
RETURNING task_id;
```

### 9.6 Checkpointer Lease Guard

Before every `put()`:
```sql
SELECT 1 FROM tasks
WHERE task_id = :task_id
  AND lease_owner = :worker_id
  AND status = 'running';
-- If 0 rows: raise LeaseRevokedException
```

### 9.7 API Validation Constants

| Field | Constraint |
|-------|-----------|
| `agent_id` | Required, max 64 chars |
| `system_prompt` | Required, max 51200 chars (50KB) |
| `model` | Must be in supported set (see below) |
| `temperature` | 0.0 - 2.0, default 0.7 |
| `allowed_tools` | Each must be in `{web_search, read_url, calculator}` |
| `input` | Required, max 102400 chars (100KB) |
| `max_retries` | 0 - 10, default 3 |
| `max_steps` | 1 - 1000, default 100 |
| `task_timeout_seconds` | 60 - 86400, default 3600 |

**Supported models:** `claude-sonnet-4-6`, `claude-sonnet-4-20250514`, `claude-haiku-4-20250514`, `gpt-4o`, `gpt-4o-mini`, `gpt-4-turbo`, `us.anthropic.claude-sonnet-4-20250514-v1:0`, `us.anthropic.claude-haiku-4-20250514-v1:0`

### 9.8 Worker Config for Tests

Use aggressive timing to keep tests fast:

| Setting | Production | E2E Test |
|---------|-----------|----------|
| `lease_duration_seconds` | 60 | 10 |
| `heartbeat_interval_seconds` | 15 | 2 |
| `reaper_interval_seconds` | 30 | 5 |
| `reaper_jitter_seconds` | 10 | 1 |
| `poll_backoff_initial_ms` | 100 | 50 |
| `poll_backoff_max_ms` | 5000 | 500 |

---

## 10. Execution

### Prerequisites
- Docker (for PostgreSQL)
- Java 21+ (for API Service: `./gradlew bootRun`)
- Python 3.11+ with worker-service dependencies: `pip install -e "services/worker-service[dev]"`
- `psql` CLI (for schema application)

### Run

```bash
# 1. Start PostgreSQL
docker run -d --name par-e2e-postgres \
  -e POSTGRES_USER=postgres -e POSTGRES_PASSWORD=postgres \
  -e POSTGRES_DB=persistent_agent_runtime -p 55432:5432 postgres:16

# 2. Apply schema
psql postgresql://postgres:postgres@localhost:55432/persistent_agent_runtime \
  < infrastructure/database/migrations/0001_phase1_durable_execution.sql

# 3. Start API service (background)
cd services/api-service && ./gradlew bootRun &
# Wait for health
until curl -sf http://localhost:8080/v1/health; do sleep 1; done

# 4. Run E2E tests (worker started in-process by test harness)
cd tests/backend-integration && python -m pytest -v
```

### CI Note
For CI, wrap steps 1-3 in a `docker-compose.yml` or GitHub Actions services block. The API service can also be containerized (requires Dockerfile from Task 7).

---

## 11. Design Doc Coverage Matrix

Maps each design doc requirement / failure scenario to the test(s) that cover it.

### Functional Requirements (F1-F10)

| Req | Description | Test(s) |
|-----|-------------|---------|
| F1 | Submit task, receive task_id | 3.1, 3.15 |
| F2 | Query status, checkpoints, cost | 3.1, 3.13 |
| F3 | Atomic claim (no double execution) | 3.16, 3.17 |
| F4 | LangGraph execution | 3.1, 3.2 |
| F5 | Each super-step checkpointed | 3.13, 3.19 |
| F6 | Crash recovery, resume from checkpoint | 3.5, 3.19, 3.20 |
| F7 | Conversation history in LangGraph state | 3.1 (implicit via multi-step) |
| F8 | Dead letter on exhausted/timeout/non-retryable | 3.7, 3.8, 3.9, 3.10 |
| F9 | Redrive | 3.11 |
| F10 | Cancel running task | 3.3, 3.4 |

### Reliability Requirements (R1-R7)

| Req | Description | Test(s) |
|-----|-------------|---------|
| R1 | Reclaim after lease expiry (bounded latency) | 3.5, 3.21 |
| R2 | No stuck running tasks | 3.5, 3.9 |
| R3 | Exponential backoff enforced by retry_after | 3.6, 3.22 |
| R4 | Infinite loops prevented by recursion_limit | 3.10 |
| R5 | max_retries prevents infinite retries | 3.7 |
| R6 | Reaper not a SPOF (all workers run it) | 3.28 |
| R7 | Lease + DB locks prevent races | 3.16, 3.17, 3.26 |

### Safety Requirements (S1-S7)

| Req | Description | Test(s) |
|-----|-------------|---------|
| S1 | Crash recovery re-executes interrupted node, tools idempotent | 3.19 |
| S2 | Tool execution restricted to allowed_tools | 3.15 (submission validation) |
| S3 | Tool argument validation | 3.15 (API), 3.8 (runtime) |
| S4 | API input validation | 3.15 |
| S5 | Secrets not in checkpoints | (code review; checkpoint content inspection deferred) |
| S6 | Tool outputs not in system prompts | (code review concern) |
| S7 | Tenant-scoped queries | 3.27 |

### Demo Requirements (D1-D4)

| Req | Description | Test(s) |
|-----|-------------|---------|
| D1 | Crash recovery demo | 3.5, 3.19 |
| D2 | Completed nodes skipped on resume | 3.19, 3.20 |
| D3 | Cost savings quantified | 3.13 (cost tracking) |
| D4 | Checkpoint history shows crash boundary | 3.19 |

### Design Doc Failure Scenarios

| Scenario | Description | Test(s) |
|----------|-------------|---------|
| Failure 1 | Worker crash + lease expiry recovery | 3.5, 3.19 |
| Failure 2 | Non-retryable node error | 3.8 |
| Failure 3 | Retryable error with backoff requeue | 3.6 |
| Failure 4 | Task cancellation during execution | 3.4 |
| Failure 5 | Redrive from dead letter | 3.11 |

### Additional Design Doc Behaviors

| Behavior | Test(s) |
|----------|---------|
| Completion check on resume (0 super-steps = already done) | 3.20 |
| Reaper: expired lease + retries exhausted -> dead letter | 3.21 |
| retry_after invisibility window | 3.22 |
| version field increments on transitions | 3.23 |
| Error fields cleared on successful completion | 3.24 |
| retry_history append-only array | 3.25 |
| LeaseRevokedException / zombie checkpointer protection | 3.26 |
| Tenant-scoped query isolation | 3.27 |
| Multi-reaper coordination (no double-reclaim) | 3.28 |

---

## 12. Priority Order

| Priority | Tests | Rationale |
|----------|-------|-----------|
| P0 | 3.1, 3.2, 3.14, 3.15 | Core lifecycle and API contract |
| P0 | 3.8, 3.9, 3.10 | Error classification correctness |
| P1 | 3.3, 3.4, 3.5 | Cancellation and crash recovery |
| P1 | 3.6, 3.7, 3.11 | Retry and redrive paths |
| P1 | 3.19, 3.20 | Checkpoint resume and crash boundary (core value prop) |
| P1 | 3.24, 3.26 | Error field hygiene, zombie protection |
| P2 | 3.12, 3.13 | Listing and checkpoint detail |
| P2 | 3.16, 3.17, 3.18 | Concurrency and multi-worker |
| P2 | 3.21, 3.22, 3.23, 3.25 | Reaper edge cases, backoff, version, retry_history |
| P3 | 3.27, 3.28 | Tenant isolation, multi-reaper coordination |
