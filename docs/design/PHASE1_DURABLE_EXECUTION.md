# Phase 1 Design — Durable Execution MVP

**Goal:** Prove that tasks survive worker crashes and resume correctly from the last checkpoint.

**Scope:** Task submission API, LangGraph-based execution engine with lease-based ownership, custom `PostgresDurableCheckpointer` for durable graph state, distributed reaper for expired leases, dead letter handling, per-node cost tracking via LangGraph event streaming, structured logging with task/worker correlation. Phase 1 supports a single top-level graph only; subgraphs are explicitly out of scope.

**Out of scope:** Agent as first-class entity (config is inline on Task), multi-agent scheduling, memory compaction, approval workflows, UI (except minimal demo dashboard), multi-tenancy, Custom Tool Runtime (BYOT — customer-provided MCP servers).

For later-phase concepts beyond Phase 1, see [PHASE2_MULTI_AGENT.md](./PHASE2_MULTI_AGENT.md) for the consolidated Phase 2 design and [DESIGN_NOTES_PHASE3_PLUS.md](./DESIGN_NOTES_PHASE3_PLUS.md) for Phase 3+ reference material.

---

## 1. Requirements

### Functional Requirements

| ID | Requirement |
|----|-------------|
| F1 | A client can submit a task with agent config and input via REST API and receive a task ID. |
| F2 | A client can query task status, checkpoint history, and cost breakdown. |
| F3 | Worker Service instances claim queued tasks from the database atomically — no two instances execute the same task simultaneously. |
| F4 | Worker Service executes the agent's logic using **LangGraph** (`StateGraph`), providing a familiar framework to AI developers. |
| F5 | Each LangGraph "super-step" (node execution) is checkpointed to the database via a custom `BaseCheckpointSaver` before the next node begins. |
| F6 | If a Worker Service instance crashes, the task is automatically reclaimed and LangGraph resumes seamlessly using the saved graph state. |
| F7 | The LangGraph state object handles conversation history natively; the runtime simply persists it durably. |
| F8 | Tasks that exhaust retries, exceed timeout, or hit non-retryable errors are moved to dead letter with full history preserved. |
| F9 | Dead-lettered tasks can be redriven (re-queued from last checkpoint). |
| F10 | A client can cancel a running task. |

### Non-Functional Requirements

**Reliability:**

| ID | Requirement |
|----|-------------|
| R1 | A crashed Worker Service instance's task is reclaimed automatically after lease expiry. With default settings (60s lease, reaper every 30s +/-10s), reclaim latency is bounded at <=100s worst-case. |
| R2 | No task is stuck in `running` indefinitely — the reaper enforces both lease expiry and total task timeout. |
| R3 | Retries use exponential backoff enforced by a `retry_after` timestamp — not just documented but schema-enforced. |
| R4 | Infinite node loops are prevented by LangGraph's `recursion_limit` (configured via `max_steps`). |
| R5 | Infinite retry loops are prevented by `max_retries` with dead letter as terminal state. |
| R6 | The reaper is not a single point of failure — every Worker Service instance runs reaper logic. |
| R7 | Execution safety relies on lease ownership and database-level locks, rather than raw optimistic concurrency, to prevent race conditions. |

**Safety:**

| ID | Requirement |
|----|-------------|
| S1 | On crash recovery, Phase 1 assumes LangGraph may re-execute the entire interrupted node (which may contain multiple tool calls). All Phase 1 tools are pre-registered, idempotent, read-only operations (`web_search`, `read_url`, `calculator`) served via a co-located MCP server, and are safe for re-execution. The `allowed_tools` whitelist is enforced at task submission — only registered idempotent tools are accepted. Non-idempotent tool guards are deferred to Phase 2 when customer-provided mutable tools are introduced via the Custom Tool Runtime. |
| S2 | Tool execution is restricted to the agent's `allowed_tools` list. |
| S3 | Tool arguments are validated against a per-tool JSON schema before execution. |
| S4 | API inputs are validated against size limits and allowed values. |
| S5 | Secrets never appear in checkpoint payloads or logs. |
| S6 | Tool outputs are treated as untrusted data and never injected into system prompts. |
| S7 | All API-facing queries are scoped by `tenant_id` to support future auth without schema changes. Internal reaper queries scan across tenants for simplicity. |

**Observability:**

| ID | Requirement |
|----|-------------|
| O1 | Every log line includes `task_id`, `worker_id`, and `node_name` for correlation. |
| O2 | Key lifecycle events are logged: task claimed, node started/completed, checkpoint saved, graph resumed, lease revoked, task completed/dead-lettered. |
| O3 | Metrics are emitted for queue depth, active tasks, node latency, cost, lease expiry rate, and empty poll frequency. |
| O4 | Alerts fire for dead letter accumulation, lease expiry spikes, worker saturation, and task age outliers. |

**Demo:**

| ID | Requirement |
|----|-------------|
| D1 | The demo proves crash recovery: a multi-step task survives a Worker Service kill and completes via a second instance. |
| D2 | The demo shows checkpoint-based resume: previously completed nodes are visibly skipped with logged cost savings. |
| D3 | The demo quantifies cost savings from checkpointing vs. re-execution from scratch. |
| D4 | Checkpoint history API shows the crash boundary — which Worker Service instance produced which checkpoints. |

---

## 2. Core Entities

In Phase 1, there is no Agent table. Agent config is snapshotted onto the Task at creation time. This simplifies the data model and eliminates the need for a separate lookup.

An Agent is data (identity, persona, memory), not a running process. It cannot "go down." A Task belongs to exactly one Agent, fixed at creation time. A Worker Service is a stateless, long-running process (deployed on ECS Fargate) that executes tasks — when it claims a task, it loads the owning agent's config from the task record and "becomes" that agent for the duration of execution. See **Section 5.0** for the full service architecture.

### Task
```
task_id:                UUID (PK)
tenant_id:              string (default "default", reserved for auth in Phase 2)
agent_id:               string (logical identifier, not a FK in Phase 1)
agent_config_snapshot:  JSON (copy of agent config at task creation time)
status:                 enum (queued | running | completed | dead_letter)
dead_letter_reason:     enum (nullable, cancelled_by_user | retries_exhausted | task_timeout | non_retryable_error | max_steps_exceeded)
worker_pool_id:         string (default "shared", reserved for Phase 2 routing)
version:                int (updated on every transition, used for auditing/ETags)
input:                  text (the task's input prompt)
output:                 JSON (final result, populated on completion)
lease_owner:            string (worker ID, null when unowned)
lease_expiry:           timestamp (null when unowned)
retry_count:            int (default 0)
max_retries:            int (default 3)
retry_after:            timestamp (null; set on retry to enforce backoff delay)
retry_history:          JSON array (default [], append-only timestamps of each retry attempt)
task_timeout_seconds:   int (default 3600)
max_steps:              int (default 100, circuit breaker)
last_error_code:        string (nullable, error classification code)
last_error_message:     string (nullable, human-readable error detail)
last_worker_id:         string (nullable, worker that last held the task before dead-lettering)
dead_lettered_at:       timestamp (nullable, when the task entered dead_letter)
created_at:             timestamp
updated_at:             timestamp
```

**Phase 1 simplifications:**
- No `waiting_for_approval` status (approval workflows are Phase 2+)
- `agent_config_snapshot` carries everything the Worker Service needs — no Agent table lookup
- `worker_pool_id` is always `"shared"` but stored for forward compatibility (Phase 2: used as tool runtime routing key for Custom Tool Runtime)
- In Phase 1, the server always resolves `tenant_id = "default"` internally for every API request. The column exists for forward auth compatibility, and all DB queries still include `tenant_id` in WHERE clauses.
- No `retrying` status — tasks go directly from `running` to `queued` (with `retry_count` incremented and `retry_after` set)
- `last_error_code` and `last_error_message` represent the task's current/latest failure context. They are cleared on successful completion and on redrive. Full retry/error event history is deferred to Phase 2+.

### Checkpoint

**Description:** Represents LangGraph's preserved execution state. At the end of every successful node execution (super-step), LangGraph serializes the agent's current memory, messages, and state variables into this table. It serves as the definitive snapshot from which a task resumes after a worker crash or retryable failure.

```
task_id:                UUID (FK -> tasks, maps to LangGraph thread_id)  ─┐
checkpoint_ns:          TEXT (default "", always root namespace in Phase 1) ├─ composite PK
checkpoint_id:          TEXT (LangGraph checkpoint_id — UUID string)       ─┘
worker_id:              TEXT (worker instance that wrote this checkpoint)
parent_checkpoint_id:   TEXT (nullable, previous checkpoint_id in the chain)
thread_ts:              TEXT (LangGraph version string, e.g. "2026-03-05T10:00:01.123456+00:00")
parent_ts:              TEXT (previous checkpoint version, nullable)
checkpoint_payload:     JSONB (serialized LangGraph Checkpoint: channel_values, channel_versions, versions_seen, pending_sends)
metadata_payload:       JSONB (LangGraph CheckpointMetadata: source, step, writes, parents)
cost_microdollars:      int (default 0, populated by cost-tracking callback)
execution_metadata:     JSONB (latency_ms, token_counts, model_used — populated by event streaming callback)
created_at:             timestamp
```

### Checkpoint Writes

**Description:** Represents LangGraph's pending, intermediate state updates associated with a checkpoint context. During execution, individual updates to specific state channels are recorded here against either the current checkpoint or the newly created checkpoint returned by `put()`. LangGraph uses this table internally to track in-flight writes and resolve state correctly during crash recovery.

```
task_id:                UUID (FK -> tasks, maps to LangGraph thread_id)
checkpoint_ns:          TEXT (default "")
checkpoint_id:          TEXT (part of composite FK -> checkpoints(task_id, checkpoint_ns, checkpoint_id))
task_path:              TEXT (LangGraph task path identifier)
idx:                    INT (write index within the checkpoint)
channel:                TEXT (LangGraph channel name)
type:                   TEXT (write type)
blob:                   BYTEA (serialized channel value)
```

LangGraph's `BaseCheckpointSaver` requires both tables. The `checkpoints` table stores the durable graph state after each completed super-step. The `checkpoint_writes` table stores pending writes used by LangGraph's checkpoint machinery during execution. In Phase 1, the runtime's safety model is conservative: if a worker crashes during a node, that node may be re-executed in full on recovery.

The database acts as a custom LangGraph `PostgresDurableCheckpointer` implementing `BaseCheckpointSaver`.
LangGraph's checkpoint metadata (`checkpoint_id`, `thread_ts`, `parent_ts`) is treated as an opaque library contract pinned to a tested version for Phase 1. Instead of a custom idempotency key, the runtime relies on the combination of durable checkpoints, lease ownership, and conservative whole-node re-execution after crash.

### Worker Service (running service — not persisted in DB)

The Worker Service is a long-running Python process deployed on ECS Fargate. Each instance generates a unique `worker_id` (e.g., `worker-{hostname}-{pid}-{uuid}`) used for lease ownership. Multiple instances run concurrently for horizontal scaling. The service is built on `asyncio` to ensure network I/O (like long LLM calls) yields execution cleanly to background tasks.

- **Task Poller:** Claims queued tasks from PostgreSQL via `FOR UPDATE SKIP LOCKED`. Uses PostgreSQL `LISTEN/NOTIFY` on the `new_task` channel to block efficiently until work is available, dropping idle DB load to near zero. Falls back to jittered polling if the connection drops.
- **Graph Executor:** An asyncio task that loads the agent's LangGraph `StateGraph`, initializes the custom `PostgresDurableCheckpointer` (constructed with the current `worker_id` and `task_id` for lease-aware writes), and executes `graph.astream(input, config={"configurable": {"thread_id": task_id}, "recursion_limit": max_steps})`. The execution is wrapped in an `asyncio.timeout(task_timeout_seconds)` block. Using `astream()` instead of `ainvoke()` gives the runtime control between super-steps — enabling mid-execution cancellation checks, cost accumulation, and circuit-breaker enforcement.
- **Co-located MCP Server:** An in-process (or sidecar) MCP server that exposes pre-registered tools (`web_search`, `read_url`, `calculator`). LangGraph tool nodes call tools via the MCP protocol. No network exposure — communication is local. See Section 5.0.3 for details.
- **Heartbeat Task:** An asyncio background task that extends the lease every 15s per active task. Running in the asyncio event loop ensures it won't be starved by long network calls in the graph executor.
- **Distributed Reaper:** Scans for expired leases and timed-out tasks on a jittered interval (30s +/-10s). Not a singleton — every Worker Service instance runs reaper logic.
- **Concurrency:** Bounded by `asyncio.Semaphore` (`MAX_CONCURRENT_TASKS`, default 10 per instance).

---

## 3. API Design

> **Covers:** F1, F2, F4, F9, F10, S4

Base path: `/v1`

**Tenant resolution in Phase 1:** Clients do not supply `tenant_id` on read, cancel, or redrive APIs. The API Service resolves `tenant_id = "default"` internally for all Phase 1 requests and still applies tenant-scoped queries in the database so auth can be added later without reshaping the schema.

### Task Submission

```
POST /v1/tasks
```

**Request:**
```json
{
  "tenant_id": "default",
  "agent_id": "support_agent_v1",
  "agent_config": {
    "system_prompt": "You are a research assistant...",
    "model": "claude-sonnet-4-6",
    "temperature": 0.7,
    "allowed_tools": ["web_search", "read_url", "calculator"]
  },
  "input": "Refund user 123 for their last order",
  "max_retries": 3,
  "max_steps": 15,
  "task_timeout_seconds": 3600
}
```

**Input validation (enforced at API layer):**

| Field | Constraint |
|-------|-----------|
| `tenant_id` | Optional, string, max 64 characters (server resolves to `"default"` in Phase 1) |
| `agent_id` | Required, string, max 64 characters |
| `input` | Required, max 100KB |
| `agent_config.system_prompt` | Required, max 50KB |
| `agent_config.model` | Required, must be in supported models list |
| `agent_config.allowed_tools` | Each tool must exist in the co-located MCP server's `listTools` response |
| `agent_config.temperature` | 0.0 - 2.0 |
| `max_retries` | 0 - 10 (default 3) |
| `max_steps` | 1 - 1000 (default 100) |
| `task_timeout_seconds` | 60 - 86400 (default 3600) |

**Response: `201 Created`**
```json
{
  "task_id": "550e8400-e29b-41d4-a716-446655440000",
  "agent_id": "support_agent_v1",
  "status": "queued",
  "created_at": "2026-03-05T10:00:00Z"
}
```

*(Note: The API purposely avoids LangGraph-specific constructs like `thread_id` or `messages` in the payload. See **[5.0.2 LangGraph Translation Layer](#502-langgraph-translation-layer)** for exactly how the Worker Service maps this generic API input into LangGraph's internal graph definitions.)*

### Task Status

```
GET /v1/tasks/{task_id}
```

**Response: `200 OK`**
```json
{
  "task_id": "550e8400-e29b-41d4-a716-446655440000",
  "agent_id": "support_agent_v1",
  "status": "running",
  "input": "Refund user 123 for their last order",
  "output": null,
  "retry_count": 0,
  "retry_history": [],
  "checkpoint_count": 5,
  "total_cost_microdollars": 12500,
  "lease_owner": "worker-abc-123",
  "last_error_code": null,
  "last_error_message": null,
  "last_worker_id": null,
  "dead_letter_reason": null,
  "dead_lettered_at": null,
  "created_at": "2026-03-05T10:00:00Z",
  "updated_at": "2026-03-05T10:00:15Z"
}
```

### Task Cancellation

```
POST /v1/tasks/{task_id}/cancel
```

**Response: `200 OK`**
```json
{
  "task_id": "550e8400-e29b-41d4-a716-446655440000",
  "status": "dead_letter",
  "dead_letter_reason": "cancelled_by_user"
}
```

Worker Service detects cancellation on the next heartbeat (lease_owner cleared or status changed). Because the runtime uses `graph.astream()`, the heartbeat coroutine sets a cancellation flag that the streaming loop checks between super-steps. If cancellation is detected between nodes, the loop exits cleanly after the last checkpoint — no partial state. 

If a long-running node (e.g., an LLM call or a slow tool execution) is in-flight:
1. Standard user cancellation takes effect after that node completes (typically < 120s for LLM calls). 
2. If the node hangs indefinitely, it will eventually hit the `asyncio.timeout(task_timeout_seconds)` wrapper dynamically configured on the Graph Executor, which will forcefully terminate the execution and send the task to the dead letter queue.

### Step History (Checkpoints)

```
GET /v1/tasks/{task_id}/checkpoints
```

Returns the ordered list of root-namespace LangGraph checkpoints for a task. Each checkpoint corresponds to a completed graph super-step (node execution). The `node_name` and `step_number` fields are derived from checkpoint metadata and insertion order.

**Response: `200 OK`**
```json
{
  "checkpoints": [
    {
      "checkpoint_id": "...",
      "step_number": 1,
      "node_name": "agent",
      "worker_id": "worker-a-123",
      "cost_microdollars": 5200,
      "execution_metadata": {
        "latency_ms": 2340,
        "input_tokens": 1250,
        "output_tokens": 340,
        "model": "claude-sonnet-4-6"
      },
      "created_at": "2026-03-05T10:00:01Z"
    },
    {
      "checkpoint_id": "...",
      "step_number": 2,
      "node_name": "tools",
      "worker_id": "worker-a-123",
      "cost_microdollars": 0,
      "execution_metadata": {
        "latency_ms": 450,
        "tools_called": ["web_search"]
      },
      "created_at": "2026-03-05T10:00:03Z"
    }
  ]
}
```

`step_number` is derived from checkpoint insertion order in the root namespace (`checkpoint_ns = ''`), not stored as a column. `node_name` is extracted from `metadata_payload.source` (the LangGraph node that produced this checkpoint). `worker_id` comes from the checkpoint row and lets the API show the crash boundary directly.

### Dead Letter

```
GET /v1/tasks/dead-letter?agent_id=support_agent_v1&limit=50
```

Returns up to `limit` dead-lettered tasks for the internally resolved Phase 1 tenant (`"default"`), ordered by `dead_lettered_at DESC, task_id DESC`. Pagination is deferred from Phase 1.

**Response: `200 OK`**
```json
{
  "items": [
    {
      "task_id": "550e8400-e29b-41d4-a716-446655440000",
      "agent_id": "support_agent_v1",
      "dead_letter_reason": "non_retryable_error",
      "last_error_code": "tool_args_invalid",
      "last_error_message": "calculator.args.expression failed validation",
      "retry_count": 1,
      "last_worker_id": "worker-a-123",
      "dead_lettered_at": "2026-03-05T10:00:20Z"
    }
  ]
}
```

```
POST /v1/tasks/{task_id}/redrive
```
Re-queues the task: resets `retry_count = 0`, sets `status = queued`. The task resumes from the last checkpoint — completed super-steps are not re-executed. Checkpoint rollback (`rollback_last_checkpoint`) is deferred to Phase 2 when mutable tools may produce checkpoints that deterministically fail on resume; Phase 1's idempotent read-only tools make this scenario unlikely.

### Health Check

```
GET /v1/health
```

**Response: `200 OK`**
```json
{
  "status": "healthy",
  "database": "connected",
  "active_workers": 3,
  "queued_tasks": 12
}
```

---

## 4. Data Flow

### Task State Machine

```
queued ──────► running ──────► completed
  ^  │           │
  │  │           ├──────► queued (if retry_count < max_retries, with retry_after set)
  │  │           │
  │  │           ├──────► dead_letter (non-retryable error OR retry_count >= max_retries)
  │  │           │
  │  │           └──────► dead_letter (timeout OR cancelled_by_user)
  │  │
  │  └──────────────────► dead_letter (timeout OR cancelled_by_user while queued)
  │
  ├──── (lease expired, reclaimed by reaper)
  │
  └──── (redriven from dead_letter via POST /redrive) ◄───── dead_letter
```

Every state transition is a conditional write relying on lease ownership (`WHERE task_id = ? AND lease_owner = ?`) or row-level locks (`FOR UPDATE SKIP LOCKED`). If two workers race, exactly one succeeds.

#### Transition Table

| From | To | Trigger | Condition |
|------|----|---------|-----------|
| queued | running | Worker Service claims task | `FOR UPDATE SKIP LOCKED`, `retry_after IS NULL OR retry_after < NOW()` |
| running | completed | Graph execution completes | Worker Service sets final output |
| running | queued | Node fails with retryable error | `retry_count < max_retries`; sets `retry_after` for backoff |
| running | dead_letter | Node fails with non-retryable error | 4xx from LLM, invalid tool, invalid tool args |
| running | dead_letter | Retryable error but exhausted | `retry_count >= max_retries` |
| `running` | `queued` | Lease expires | Reaper reclaims, increments retry_count, sets retry_after |
| `running` / `queued` | `dead_letter` | Task timeout exceeded | Reaper detects `created_at + task_timeout_seconds < NOW()` |
| `running` / `queued` | `dead_letter` | Task canceled by user | API receives `POST /cancel` |
| `dead_letter` | `queued` | Task redrive | API receives `POST /redrive`; resets `retry_count` and `retry_after` |
### Sequence Diagrams

#### Task Submission

```
Client                    API Service             PostgreSQL
  |                          |                        |
  +-- POST /v1/tasks ------->|                        |
  |                          +-- Validate input ------>|
  |                          +-- INSERT task --------->|
  |                          |  (status=queued)        |
  |                          |<-- task_id -------------+
  |<-- 201 {task_id} --------+                        |
```

*(LISTEN/NOTIFY omitted for clarity — see Section 5.3)*

#### Task Claim + Graph Execution (Happy Path)

```
Worker Service                PostgreSQL              LLM API       MCP Server
  |                              |                      |                |
  +-- Poll: CTE claim query ---->|                      |                |
  |  (FOR UPDATE SKIP LOCKED)    |                      |                |
  |<-- task row (status=running) +                      |                |
  |                              |                      |                |
  |  [Start heartbeat asyncio task]                     |                |
  |                              |                      |                |
  +-- graph.astream(thread_id) --+                      |                |
  |  (LangGraph execution)       |                      |                |
  |                              |                      |                |
  |-- Node Execution ------------+                      |                |
  |  (e.g., llm_call node)       |                      |                |
  +-- LLM call ---------------------------------------->|                |
  |<-- LLM response (tool_calls) -----------------------+                |
  |                              |                      |                |
  |-- Custom Checkpointer Saves -+                      |                |
  +-- INSERT checkpoint -------->|                      |                |
  |  + checkpoint_payload        |                      |                |
  |                              |                      |                |
  |  [LangGraph routes to tools] |                      |                |
  |                              |                      |                |
  |-- Node Execution ------------+                      |                |
  |  (e.g., tool_execution node) |                      |                |
  +-- MCP tool call (e.g. web_search) ----------------------------------->|
  |<-- Tool result (via MCP protocol) -----------------------------------+
  |                              |                      |                |
  |-- Custom Checkpointer Saves -+                      |                |
  +-- INSERT checkpoint -------->|                      |                |
  |  + checkpoint_payload        |                      |                |
  |                              |                      |                |
  |  [LangGraph auto-loops to next node]                |                |
  |  ... repeat until done ...   |                      |
  |                              |                      |
  +-- graph.astream exhausted ---+                      |
  +-- UPDATE task (completed) -->|                      |
  |  [Stop heartbeat task]       |                      |
```

*(LISTEN/NOTIFY omitted for clarity — see Section 5.3)*

#### Heartbeat (runs concurrently with graph execution)

```
Worker Service                PostgreSQL
  |                              |
  |  [Every 15 seconds]          |
  +-- UPDATE lease_expiry ------>|
  |  WHERE lease_owner=me        |
  |  AND status='running'        |
  |  (no version check)          |
  |<-- rows_affected ------------+
  |                              |
  |  If rows_affected == 0:      |
  |  -> Lease revoked, STOP      |
```

#### Failure Scenario 1: Worker Crash & Lease Expiry Recovery

This diagram illustrates what happens when a worker claims a task, connects to the LLM, and then abruptly crashes before the checkpointer can save the state.

This also illustrates an **"Idempotency Hit"**. The LLM performed work (and billed for tokens!) but the checkpoint was never saved. When the new worker resumes the graph, it must execute that exact same LLM API call again.

```
Worker 1              LLM API       PostgreSQL              Worker 2 (Reaper)
  |                      |              |                      |
  +-- Claims Task ------>|              |                      |
  |  (status=running)    |              |                      |
  |                      |              |                      |
  +-- LLM call --------->|              |                      |
  |                      |              |                      |
[CRASH]                  |              |                      |
  x                      |              |                      |
                         |              |                      |
                       (Time passes, lease_expiry is explicitly missed)
                         |              |                      |
                         |              |<-- Reaper Poll ------+
                         |              |    (Finds lease_expiry < NOW())
                         |              |                      |
                         |              |<-- UPDATE task ------+
                         |              |  (status=queued,     |
                         |              |   retry_count++,     |
                         |              |   retry_after=NOW+X) |
```

*Note: When a completely fresh worker eventually claims this queued task, LangGraph's `astream` will load the last successfully saved checkpoint from the DB and transparently repeat the LLM call that Worker 1 initiated before crashing.*

#### Failure Scenario 2: Non-Retryable Node Error

This diagram illustrates what happens when LangGraph attempts to execute a tool, but the tool execution throws a fatal error (e.g., the LLM hallucinated arguments that fail Pydantic validation) that should not be retried.

```
Worker Service                MCP Server              PostgreSQL
  |                                |                        |
  +-- MCP tool call (calculator) ->|                        |
  |                                |                        |
  |<-- ValidationException --------+                        |
  |  (e.g., division by zero)      |                        |
  |                                |                        |
  |  [Worker catches Exception]    |                        |
  |  [Determines non-retryable]    |                        |
  |                                |                        |
  +-- UPDATE task ----------------------------------------->|
  |  (status=dead_letter,                                   |
  |   dead_letter_reason=non_retryable_error)               |
  |                                |                        |
  |  [Stop graph.astream]          |                        |
  |  [Stop heartbeat]              |                        |
```

#### Failure Scenario 3: Retryable Error with Backoff Requeue

This diagram illustrates what happens when an LLM call returns a transient error (e.g., 5xx or 429 rate limit). The worker catches the error, classifies it as retryable, and requeues the task with an exponential backoff delay. The task remains invisible to workers until `retry_after` expires.

```
Worker Service                LLM API               PostgreSQL
  |                              |                      |
  +-- LLM call ----------------->|                      |
  |                              |                      |
  |<-- 503 Service Unavailable --+                      |
  |                              |                      |
  |  [Catches retryable error]   |                      |
  |  [retry_count < max_retries] |                      |
  |                              |                      |
  +-- UPDATE task -------------------------------------->|
  |  (status=queued,                                    |
  |   retry_count++,                                    |
  |   retry_after=NOW + 2^retry_count seconds,          |
  |   lease_owner=NULL)                                 |
  |                              |                      |
  |  [Stop graph.astream]        |                      |
  |  [Stop heartbeat]            |                      |
  |                              |                      |
  |                          (Time passes, retry_after expires)
  |                              |                      |
  |                              |      Another Worker  |
  |                              |           |          |
  |                              |           +-- Claim ->|
  |                              |           |  (retry_after < NOW())
  |                              |           |<- task ---+
  |                              |           |          |
  |                              |  [Resumes from last checkpoint]
```

#### Failure Scenario 4: Task Cancellation During Execution

This diagram illustrates the between-node cancellation path. The API sets the task to `dead_letter` and clears the lease. The worker detects this on its next heartbeat and exits cleanly after the current node finishes. If a long-running node (e.g., an LLM call or slow tool execution) is in-flight, cancellation takes effect after that node completes. If the node hangs indefinitely, it will eventually be caught by the Graph Executor's hard `asyncio.timeout` wrapper. The checkpoint is always clean.

```
Client              API Service          PostgreSQL              Worker Service
  |                    |                      |                      |
  +-- POST /cancel --->|                      |                      |
  |                    +-- UPDATE task ------->|                      |
  |                    |  (status=dead_letter, |                      |
  |                    |   lease_owner=NULL,   |                      |
  |                    |   dead_letter_reason= |                      |
  |                    |   cancelled_by_user)  |                      |
  |<-- 200 OK ---------+                      |                      |
  |                    |                      |                      |
  |                    |                      |   [Node in-flight     |
  |                    |                      |    e.g., LLM call]    |
  |                    |                      |                      |
  |                    |                      |   [Heartbeat fires]   |
  |                    |                      |<-- UPDATE lease_expiry+
  |                    |                      |    WHERE lease_owner=me
  |                    |                      |    AND status='running'
  |                    |                      +-- rows_affected=0 --->|
  |                    |                      |                      |
  |                    |                      |   [Lease revoked]     |
  |                    |                      |   Sets cancellation   |
  |                    |                      |   flag. Current node  |
  |                    |                      |   finishes. Streaming |
  |                    |                      |   loop checks flag,   |
  |                    |                      |   exits cleanly.      |
  |                    |                      |   [Stop heartbeat]    |
```

#### Failure Scenario 5: Redrive from Dead Letter

This diagram illustrates redriving a dead-lettered task. The API resets the task to `queued` and clears dead-letter fields. The next worker that claims it resumes from the last checkpoint. Checkpoint rollback is deferred to Phase 2.

```
Client              API Service          PostgreSQL              Worker Service
  |                    |                      |                      |
  +-- POST /redrive -->|                      |                      |
  |                    +-- UPDATE task ------->|                      |
  |                    |  (status=queued,      |                      |
  |                    |   retry_count=0,      |                      |
  |                    |   clear dead_letter   |                      |
  |                    |   fields)             |                      |
  |<-- 200 OK ---------+                      |                      |
  |                    |                      |                      |
  |                    |                      |      [Worker claims]  |
  |                    |                      |<-- FOR UPDATE --------+
  |                    |                      |    SKIP LOCKED        |
  |                    |                      +-- task row ---------->|
  |                    |                      |                      |
  |                    |                      |  [graph.astream loads |
  |                    |                      |   last checkpoint,    |
  |                    |                      |   resumes from there] |
```

---

## 5. High-Level Design

### 5.0 Services & Deployment Overview

> **Covers:** F1, F3, F4, R6

Phase 1 has three deployable components. Understanding their boundaries is essential before diving into execution details.

| Service | Runtime | Responsibility | Stateful? |
|---------|---------|---------------|-----------|
| **API Service** | Java (Spring Boot) on ECS Fargate | REST API — accepts task submissions, serves status/history queries, handles cancellation and redrive | No (all state in PostgreSQL) |
| **Worker Service** | Python on ECS Fargate | Polls for queued tasks, executes LangGraph (LLM calls + tool calls via co-located MCP server), heartbeats, runs distributed reaper | No (all state in PostgreSQL) |
| **PostgreSQL** | Aurora Serverless v2 | State store, task queue, LangGraph checkpoints, conversation history | Yes (source of truth) |

The **API Service** and **Worker Service** are independent processes that share nothing except the database. Multiple instances of each can run concurrently. Neither holds in-memory state that would be lost on crash — all durable state lives in PostgreSQL.

A single **Worker Service** instance contains several concurrent subsystems:

- **Task Poller** — polls PostgreSQL for claimable tasks using `FOR UPDATE SKIP LOCKED`
- **Graph Executor** — initializes the LangGraph agent, injects the `PostgresDurableCheckpointer`, and calls `astream()` with `recursion_limit`
- **Heartbeat Task** — extends lease every 15s per active task (independent of graph execution)
- **Distributed Reaper** — scans for expired leases and timed-out tasks on a jittered interval

Phase 1 is validated against these pinned package versions:
- `langgraph==1.0.5`
- `langgraph-checkpoint==4.0.0`
- `langgraph-checkpoint-postgres==3.0.4`

#### 5.0.1 Architectural Decision: PostgreSQL vs SQS for Queueing

For Phase 1, "Database-as-a-Queue" (PostgreSQL) is deliberately chosen over a dedicated message broker like AWS SQS.

**Rationale:**
1. **Atomic Ingestion (No Dual-Write Problem):** When a user calls `POST /v1/tasks`, inserting the task with `status = 'queued'` is a single atomic database transaction. If SQS were used, the system would need to insert into PostgreSQL *and* `SendMessage` to SQS. If one succeeds and the other fails, the task is orphaned or data is missing.
2. **Unified State and Leasing:** AWS SQS guarantees "at-least-once" delivery, meaning duplicate messages are possible. To prevent two workers from concurrently executing the same non-deterministic LLM agent, a database lease (lock) is still required. Since the database must maintain the lease state to prevent corruption, having the worker claim the lease and read the task state in a single atomic SQL query (`FOR UPDATE SKIP LOCKED`) drastically simplifies the orchestration.
3. **Implementation Speed:** PostgreSQL eliminates the need for AWS infrastructure setup (IAM roles, polling loops, visibility timeouts) during local development and reduces distributed systems complexity.

**Evolution:** SQS (or a similar message broker) will be introduced in later phases via a Transactional Outbox pattern when PostgreSQL connection limits or CPU become a bottleneck under extremely high concurrency. For Phase 1's goal of proving the durable runloop, PostgreSQL is the robust, highly-durable choice.

#### System Context Diagram

Shows the three services, external actors, and how they connect.

```
                         ┌─────────────────────────────────────────┐
                         │           External Systems               │
                         │                                          │
                         │  ┌─────────────┐    ┌────────────────┐  │
                         │  │  LLM APIs   │    │  External APIs  │  │
                         │  │  (Bedrock,  │    │  (Tavily, etc.) │  │
                         │  │  Anthropic)  │    │                │  │
                         │  └──────▲──────┘    └──────▲─────────┘  │
                         └─────────┼──────────────────┼────────────┘
                                   │                  │
                                   │ HTTPS            │ HTTPS
                                   │                  │
┌──────────┐  REST    ┌───────────┴──────────────────┼───────────┐
│          │  (HTTPS) │                              │            │
│  Client  │ ────────>│              API Service     │            │
│          │ <────────│         (Java / Spring Boot) │            │
│          │          │                              │            │
└──────────┘          │  POST /v1/tasks      GET /v1/tasks/{id}  │
                      │  POST /cancel        GET /checkpoints     │
                      │  POST /redrive       GET /dead-letter     │
                      └──────────────┬────────────────────────────┘
                                     │
                                     │ SQL (read/write)
                                     │
                              ┌──────▼──────┐
                              │             │
                              │ PostgreSQL  │
                              │ (Aurora v2) │
                              │             │
                              └──────▲──────┘
                                     │
                                     │ SQL (read/write)
                                     │
┌────────────────────────────────────┴────────────────────────────┐
│                                                                  │
│                     Worker Service (Python)                       │
│                     x N instances on ECS Fargate                  │
│                                                                  │
│  ┌─────────────┐  ┌──────────────┐  ┌───────────────────────┐   │
│  │ Task Poller  │  │  Heartbeat   │  │  Distributed Reaper   │   │
│  │             │  │  Task        │  │  (jittered interval)  │   │
│  └──────┬──────┘  └──────┬───────┘  └───────────┬───────────┘   │
│         │                │                      │                │
│         ▼                │                      │                │
│  ┌─────────────────┐     │                      │                │
│  │ Graph Executor   │     │                      │                │
│  │                  │     │                      │                │
│  │  ┌────────────┐ │     │                      │                │
│  │  │  LangGraph │ │─────┼── LLM API calls ──────────────────>  │
│  │  │  astream() │ │     │                                       │
│  │  │  (agent +  │ │     │  ┌──────────────────────────────┐    │
│  │  │   tools)   │ │─────┼─>│  Co-located MCP Server       │    │
│  │  └────────────┘ │     │  │  (in-process / sidecar)      │    │
│  └─────────────────┘     │  │  web_search, read_url,       │────┼── External API calls ──>
│                          │  │  calculator                   │    │
│                          │  └──────────────────────────────┘    │
│                          │                                       │
└──────────────────────────┴───────────────────────────────────────┘
```

#### Worker Service Internal Architecture

A single Worker Service instance runs these subsystems concurrently. Each subsystem operates independently — the heartbeat task keeps the lease alive while the graph executor blocks on a slow LLM call.

```
┌──────────────────────────────────────────────────────────────────┐
│                     Worker Service Instance                       │
│                     worker_id: worker-{host}-{pid}-{uuid}        │
│                                                                   │
│  ┌──────────────────────────────────────────────────────────┐    │
│  │                    Task Poller                             │    │
│  │                                                           │    │
│  │  - Polls DB: FOR UPDATE SKIP LOCKED                       │    │
│  │  - Backoff: 100ms -> 200ms -> ... -> 5s cap (on empty)    │    │
│  │  - Bounded concurrency: semaphore (MAX_CONCURRENT=10)     │    │
│  └─────────────────────────┬─────────────────────────────────┘    │
│                            │ claimed task                          │
│                            ▼                                      │
│  ┌──────────────────────────────────────────────────────────┐    │
│  │                   Graph Executor (per task)               │    │
│  │                                                           │    │
│  │  1. Init Custom PostgresDurableCheckpointer               │    │
│  │  2. Load LangGraph StateGraph (agent_config)              │    │
│  │  3. Wrap with asyncio.timeout(task_timeout_seconds)       │    │
│  │  4. Execute: graph.astream(.., thread_id=task_id)         │    │
│  │                                                           │    │
│  │     ┌─────────────┐    ┌─────────────────┐                │    │
│  │     │ LangGraph   │    │ Checkpointer    │                │    │
│  │     │ Node        │    │                 │                │    │
│  │     │ Execution   │    │ Saves state to  │                │    │
│  │     │ (LLM/Tools) │────│ PostgreSQL      │                │    │
│  │     │      │      │    │ after nodes     │                │    │
│  │     └──────┼──────┘    └─────────────────┘                │    │
│  │            │ MCP protocol (local)                         │    │
│  │            ▼                                              │    │
│  │     ┌─────────────────────┐                               │    │
│  │     │ Co-located MCP      │── HTTPS ──> External APIs     │    │
│  │     │ Server (in-process) │            (Tavily, etc.)     │    │
│  │     │ web_search, read_url│                               │    │
│  │     │ calculator          │                               │    │
│  │     └─────────────────────┘                               │    │
│  │  5. Catch errors/timeouts -> propagate to Dead Letter     │    │
│  │  6. Return final output -> Complete task                  │    │
│  └──────────────────────────────────────────────────────────┘    │
│                                                                   │
│  ┌─────────────────────────┐  ┌──────────────────────────────┐   │
│  │   Heartbeat Task         │  │    Distributed Reaper         │   │
│  │   (per active task)      │  │    (shared across workers)    │   │
│  │                          │  │                               │   │
│  │  Every 15s:              │  │  Every 30s +/-10s jitter:     │   │
│  │  UPDATE lease_expiry     │  │                               │   │
│  │  WHERE lease_owner=me    │  │  1. Reclaim expired leases    │   │
│  │  AND status='running'    │  │     (lease_expiry < NOW)      │   │
│  │                          │  │  2. Dead-letter if retries    │   │
│  │  If 0 rows affected:    │  │     exhausted                 │   │
│  │  -> lease revoked, STOP  │  │  3. Dead-letter if task       │   │
│  │                          │  │     timeout exceeded          │   │
│  └─────────────────────────┘  └──────────────────────────────┘   │
└──────────────────────────────────────────────────────────────────┘
```

#### 5.0.2 LangGraph Translation Layer

The REST API request deliberately hides LangGraph-specific constructs (like `thread_id` or `messages`) to provide a clean, agnostic boundary for the client. When the Worker Service claims a task in the **Task Poller**, it passes the generic API payload to the **Graph Executor**, which translates it into the specific objects required by LangGraph before calling `astream()`:

1. **`agent_config` $\rightarrow$ Graph Definition:** The worker uses the config to instantiate the graph components dynamically. It configures the LLM backend (e.g., `ChatAnthropic` with `model` and `temperature`), binds the `allowed_tools`, and defines the exact `SystemMessage` node based on the `system_prompt`.
2. **`input` $\rightarrow$ Initial State:** The generic `input` text is converted into the strongly-typed starting state for the graph, typically wrapping it in a `HumanMessage` object and pushing it into the graph's `messages` channel.
3. **`task_id` $\rightarrow$ `thread_id`:** The UUID generated by the API (`task_id`) is passed to LangGraph inside the `RunnableConfig` object as the `thread_id`. This is exactly how the `PostgresDurableCheckpointer` links the graph state back to the task record in the database.
4. **`max_steps` $\rightarrow$ `recursion_limit`:** The API parameter `max_steps` overrides LangGraph's default `recursion_limit` to prevent infinite tool-calling loops.
5. **Worker Config $\rightarrow$ Checkpoint Namespace:** While Phase 1 always uses the root namespace (`""`), the translation layer ensures the `PostgresDurableCheckpointer` is initialized with the current `worker_id` so that inserted checkpoints accurately record the crash boundary.

#### 5.0.3 Pre-Registered Tools via Internal MCP Server (Phase 1)

In Phase 1, the Custom Tool Runtime (BYOT) is not yet supported — customers cannot provide their own tools. The Worker Service ships with a **co-located MCP server** that exposes a standard library of pre-registered tools. The MCP server runs in-process (or as a sidecar) within the Worker Service container — no network exposure.

Using MCP as the tool interface even in Phase 1 establishes the abstraction boundary early. In Phase 2, the same MCP protocol is used to call customer-provided MCP servers running in isolated containers within the platform's VPC — no Worker Service changes needed.

When a task is submitted, the `allowed_tools` array in the `agent_config` is validated against the tools advertised by the internal MCP server's `listTools` response. If an unknown tool is requested, the task immediately fails validation.

The initial MVP toolkit includes:
1. **`web_search`:** Executes a search engine query (e.g., via Tavily or Serper API) and returns top results.
2. **`read_url`:** Fetches the raw HTML of a given URL, strips markup, and returns clean markdown content.
3. **`calculator`:** Evaluates safe mathematical expressions to prevent the LLM from hallucinating arithmetic.

Because the runtime treats tool execution conservatively (re-executing the entire node on crash recovery, see Requirement **S1**), these built-in tools are designed strictly as **idempotent, read-only** operations. Tools with mutable side-effects (like writing to a database or sending an email) are deferred to Phase 2, where the Custom Tool Runtime runs customer-provided MCP servers in isolated compute and the control plane enforces non-idempotent tool guards (checkpoint-before-call, dead-letter on re-execution after crash).

#### Deployment View

```
┌─────────────────────────────────────────────────────────────┐
│                        AWS Account                           │
│                                                              │
│  ┌────────────────────────────────┐                          │
│  │         ECS Cluster            │                          │
│  │                                │                          │
│  │  ┌──────────────────────────┐  │                          │
│  │  │  API Service (Fargate)   │  │     ┌──────────────┐    │
│  │  │  x2 tasks (HA)           │──┼────>│              │    │
│  │  └──────────────────────────┘  │     │  Aurora       │    │
│  │                                │     │  Serverless   │    │
│  │  ┌──────────────────────────┐  │     │  v2           │    │
│  │  │ Worker Service (Fargate) │──┼────>│  (PostgreSQL) │    │
│  │  │  x N tasks (scale on     │  │     │              │    │
│  │  │   queue depth)           │  │     └──────────────┘    │
│  │  │                          │  │                          │
│  │  │  ┌────────────────────┐  │  │                          │
│  │  │  │ Co-located MCP     │  │  │                          │
│  │  │  │ Server (in-process)│  │  │                          │
│  │  │  └──────────┬─────────┘  │  │                          │
│  │  └─────────────┼────────────┘  │                          │
│  │                │               │                          │
│  └────────────────┼───────────────┘                          │
│                   │ HTTPS                                    │
│                   ▼                                          │
│  ┌─────────────────────────┐  ┌──────────────────────────┐  │
│  │  Bedrock (LLM calls)    │  │  CloudWatch (logs +      │  │
│  │                          │  │  metrics via OTel)       │  │
│  ├─────────────────────────┤  └──────────────────────────┘  │
│  │  External APIs (Tavily,  │                                │
│  │  web search backends)    │                                │
│  └─────────────────────────┘                                │
│                                                              │
└─────────────────────────────────────────────────────────────┘
```

#### Task Lifecycle Overview

A non-sequential view of how a task flows through the system from submission to completion (or dead letter).

```
                    Client
                      │
                      │ POST /v1/tasks
                      ▼
               ┌──────────────┐
               │  API Service  │
               │               │
               │  Validate     │
               │  Insert task  │
               │  (queued)     │
               └──────┬───────┘
                      │
                      ▼
            ┌─────────────────┐
            │   PostgreSQL     │
            │                  │
            │  tasks table:    │
            │  status=queued   │◄─────────────── Redrive
            └────────┬────────┘                  (POST /redrive)
                     │                                ▲
          ┌──────────┴──────────┐                     │
          │  Worker Service     │                     │
          │  claims task        │                     │
          │  (FOR UPDATE        │                     │
          │   SKIP LOCKED)      │                     │
          └──────────┬──────────┘                     │
                     │                                │
                     ▼                                │
          ┌──────────────────┐                        │
          │  LangGraph Loop   │                        │
          │                   │                        │
          │  ┌─────────────┐  │                        │
          │  │ llm_call    ├──┼── LLM API              │
          │  └──────┬──────┘  │                        │
          │         │         │                        │
          │         ▼         │                        │
          │  tool_calls in    │                        │
          │  response?        │                        │
          │   │yes     │no    │                        │
          │   ▼        ▼      │                        │
          │  ┌──────┐ DONE    │                        │
          │  │tool_ │         │                        │
          │  │call  │         │                        │
          │  └──┬───┘         │                        │
          │     │             │                        │
          │     └─── next ────┘                        │
          │      llm_call                              │
          └─────────┬─────────┘                        │
                    │                                  │
          ┌────────┬┴────────┬──────────┐              │
          ▼        ▼         ▼          ▼              │
     ┌────────┐ ┌───────┐ ┌───────┐ ┌────────┐        │
     │COMPLETED│ │ Error │ │ Error │ │Crash/  │        │
     │        │ │(retry)│ │(fatal)│ │Timeout │        │
     └────────┘ └───┬───┘ └───┬───┘ └───┬────┘        │
                    │         │         │              │
                    ▼         │    Reaper detects      │
               ┌────────┐    │    expired lease        │
               │Re-queue │    │         │              │
               │(backoff)│    │         ▼              │
               │retry_   │    │    ┌────────┐          │
               │count++  ├────┼───>│Dead    │          │
               └────┬────┘    │    │Letter  ├──────────┘
                    │         └───>│        │
                    │              └────────┘
              (if retries
               exhausted)
```

### 5.1 Database-as-Queue (Dual-Write Elimination)

> **Covers:** F1, F3, R7

**Problem:** A typical architecture has separate database and queue systems. Writing to both creates a dual-write problem — if one write succeeds and the other fails, tasks are either lost or orphaned.

**Solution: Use the database as the queue for Phase 1.**

1. Client submits task via **API Service**
2. API Service stores task in database with `status = queued` (single atomic write)
3. **Worker Service** polls database for queued tasks and claims one atomically (using `FOR UPDATE SKIP LOCKED`)
4. Worker Service initializes LangGraph with the `PostgresDurableCheckpointer` and calls `graph.astream()` — LangGraph loads the last checkpoint (if any) and resumes from that state
5. Each super-step (node execution) is checkpointed by LangGraph via the custom checkpointer
6. Worker Service heartbeats every 15s to extend lease
7. Repeat until task reaches terminal state or timeout

One system, one write, no dual-write risk. PostgreSQL handles this pattern up to ~5,000-10,000 claims/sec, which is sufficient through Phase 2. For Phase 2+, if throughput demands it: transactional outbox pattern with SQS FIFO (`agent_id` as message group ID for per-agent ordering).

### 5.2 Checkpoint-Resume Execution Model

> **Covers:** F5, F6, F7

**Problem:** Temporal-style replay requires deterministic orchestration logic — the same inputs must produce the same step sequence. AI agents violate this: the LLM decides the next step, and LLM output varies per call.

**Solution:** This runtime uses **checkpoint-resume**, not event-sourced deterministic replay. LangGraph loads the last checkpoint (which contains the full graph state needed to continue) and resumes execution from there. No event log replay. No determinism constraints.

**Tradeoff:** Checkpoint-resume cannot "time-travel" through execution history. If you need to understand why a node produced a certain result, you inspect the stored checkpoint, not replay from the beginning. For AI agents (where replaying would produce different results anyway), this tradeoff is clearly correct.

### 5.3 Lease Protocol & Crash Recovery

> **Covers:** F3, F6, F10, R1, R2, R6, R7

**Problem:** Without leases, crashed workers orphan tasks (stuck in `running` forever) or two workers execute the same task simultaneously.

**Mechanism:**
- Worker Service acquires a 60-second lease on task claim (conditional write)
- Worker Service starts a heartbeat task (separate from graph execution) that extends the lease by 60s every 15s
- If heartbeat update returns 0 rows, the lease was revoked — Worker Service stops execution immediately
- LLM calls taking 5-120s: heartbeat task runs independently, keeps extending the lease

**Important:** The heartbeat checks `lease_owner` and `status` only — **not** `version`. `tasks.version` changes on task lifecycle transitions, not checkpoint persistence, so heartbeats should not couple lease ownership to a version snapshot.

**Worker Service Claiming (LISTEN/NOTIFY):** To avoid spin-polling and high CPU/DB load when the queue is empty, the system uses PostgreSQL's pub/sub capabilities:
- **Queue-entry transitions:** Any transition that makes a task claimable (`status='queued'`) emits `NOTIFY new_task, '<pool_id>'` in the same transaction (`POST /tasks`, retry requeue, reaper reclaim, redrive).
- **Worker Service:** Blocks efficiently on `LISTEN new_task`. When notified, it executes the `FOR UPDATE SKIP LOCKED` claim query.
- **Fallback:** If the connection drops, a worker restarts, or a NOTIFY is missed, workers fall back to periodic polling before listening again. The metric `poll.empty` tracks how often the worker wakes up but finds no task (e.g., due to concurrent workers grabbing the task first).

**Distributed Reaper:** The reaper is **not** a single background process (which would be a SPOF). Instead, every Worker Service instance runs reaper logic on a jittered interval (default: every 30s +/- 10s jitter). The reaper scans for:
1. **Expired leases** — tasks where `lease_expiry < NOW()`. Re-queues with incremented `retry_count` and exponential backoff, or dead-letters if retries exhausted.
2. **Task timeouts** — tasks where `created_at + task_timeout_seconds < NOW()`. Transitions directly to `dead_letter`.

With the default timings above, reclaim latency after a crash is bounded by lease duration + max reaper interval gap (`60s + 40s = 100s` worst-case).

All reaper operations use `UPDATE ... RETURNING` instead of SELECT-then-UPDATE to avoid TOCTOU races between multiple reapers. The conditional write ensures exactly one reaper reclaims each task.

Any code path that increments `retry_count` (reaper reclaim or retryable node failure) also appends `NOW()` to `retry_history`.

### 5.4 LangGraph Execution & Checkpointing

> **Covers:** F4, F5

In Phase 1, the runtime utilizes **LangGraph** to process the operational logic of the agent. By relying on LangGraph beneath the hood, the project natively supports advanced multi-actor workflows, robust state management, and familiar developer APIs.

**The Role of the Worker:**
The Worker Service acts as the host environment. Once it claims a task, it dynamically builds the LangGraph `StateGraph` based on the `agent_config_snapshot`. 

**The Checkpointer Database Adapter:**
Instead of storing raw inputs/outputs, the DB acts as the `BaseCheckpointSaver`.
When `graph.astream()` completes a node (a "super-step"), LangGraph calls the Checkpointer's `put()` method, which serializes the graph state and inserts a row into the database.

This enables seamless resumption. If the worker crashes, the new worker instantiates the graph and simply calls `graph.astream()` with the same `thread_id`. LangGraph's internal logic loads the last saved state from the database via the Checkpointer and resumes from that exact position.

#### LangGraph Data Flow

The following diagram illustrates how the Worker Service integrates LangGraph with the PostgreSQL backend during a single task run.

```text
 ┌───────────────────────────────────────────────────────────────────┐
 │                         DATA SOURCES                              │
 │                                                                   │
 │  tasks table                    checkpoints table                 │
 │  ┌─────────────────────┐        ┌──────────────────────────┐      │
 │  │ agent_config_snapshot│        │ completed checkpoints    │      │
 │  │  ├─ system_prompt    │        │  (ordered by created_at  │      │
 │  │  ├─ model            │        │   / insertion order)     │      │
 │  │  ├─ allowed_tools    │        │  ├─ checkpoint_payload   │      │
 │  │  └─ temperature      │        │  └─ metadata             │      │
 │  └──────────┬──────────┘        └────────────┬─────────────┘      │
 │             │                                │                    │
 └─────────────┼────────────────────────────────┼────────────────────┘
               │                                │
               ▼                                ▼
 ┌───────────────────────────────────────────────────────────────────┐
 │                     LANGGRAPH EXECUTION                           │
 │                     (Worker Service)                              │
 │                                                                   │
 │  ┌─────────────────────────────────────────────────────────┐      │
 │  │ 1. Graph State Init:  ◄── agent_config + checkpoint     │      │
 │  │                       (LangGraph loads prior graph state)│      │
 │  └─────────────────────────────────────────────────────────┘      │
 │                          │                                        │
 │           ┌──────────────┴──────────────┐                         │
 │           │                             │                         │
 │           ▼                             ▼                         │
 │ ┌──────────────────────┐      ┌──────────────────────┐            │
 │ │   LLM Node (Agent)   │      │   Tool Node (Action) │            │
 │ │                      │      │                      │            │
 │ │  Graph State ────►   │      │  Tool Call ────────► │            │
 │ │  LLM API (Bedrock /  │      │  MCP Server (local)  │            │
 │ │  Anthropic / OpenAI) │      │                      │            │
 │ │                      │      │  ◄── Tool Result     │            │
 │ │  ◄── Response        │      │                      │            │
 │ │   (updates state)    │      │   (updates state)    │            │
 │ └─────────┬────────────┘      └─────────┬────────────┘            │
 │           │                             │                         │
 │           └─────────────┬───────────────┘                         │
 │                         │                                         │
 │                         ▼                                         │
 │  ┌─────────────────────────────────────────────────────────┐      │
 │  │ 2. Checkpoint Save:                                     │      │
 │  │ LangGraph emits the new state object to the             │      │
 │  │ PostgresDurableCheckpointer.                            │      │
 │  └─────────────────────────────────────────────────────────┘      │
 │                          │                                        │
 └──────────────────────────┼────────────────────────────────────────┘
                            │
                            ▼
 ┌───────────────────────────────────────────────────────────────────┐
 │                         DATA SINKS                                │
 │                                                                   │
 │  checkpoints table (INSERT)         tasks table (UPDATE)          │
 │  ┌───────────────────────┐          ┌──────────────────────┐      │
 │  │ worker_id             │          │ status transitions   │      │
 │  │ checkpoint_payload    │          │ version++            │      │
 │  │ metadata_payload      │          │ updated_at = NOW()   │      │
 │  │ cost_microdollars     │          │                      │      │
 │  │ execution_metadata:   │          │ (if final node:      │      │
 │  │   latency_ms,         │          │  status='completed', │      │
 │  │   token_counts,       │          │  output=last_state)  │      │
 │  │   model_used          │          └──────────────────────┘      │
 │  │ created_at = NOW()    │                                        │
 │  └───────────────────────┘                                        │
 │                                                                   │
 │  The 3-phase execution cycle repeats until LangGraph completes.   │
 └───────────────────────────────────────────────────────────────────┘
```

By moving the loop to LangGraph, the Worker Service is dramatically simplified. It no longer implements `llm_call` or `tool_call` handlers directly. It streams super-step events from LangGraph and lets the checkpointer handle state persistence.

#### Cost & Execution Metadata Tracking

Cost tracking and execution metadata are collected via **LangGraph callback handlers**, not through the checkpointer:

1. A custom `CostTrackingCallback` is registered with the LangGraph invocation. It subscribes to `on_llm_end` events to capture token usage (`input_tokens`, `output_tokens`, `model`), calculates `cost_microdollars` from a static price-per-model config map, and records `latency_ms`.
2. After each super-step completes (detected via the `astream()` loop), the Worker Service writes the accumulated cost and metadata to the checkpoint row for that super-step via a separate `UPDATE checkpoints SET cost_microdollars = ..., execution_metadata = ... WHERE checkpoint_id = ...` statement.
3. The checkpointer's `put()` method itself only writes the LangGraph state — it does not handle cost or metadata. This keeps the checkpointer implementation clean and compatible with the `BaseCheckpointSaver` interface.

This metadata write is intentionally decoupled from checkpoint persistence. Recovery correctness depends only on the checkpoint row itself; cost and execution metadata are observability fields. If a worker crashes after the checkpoint commit but before the metadata update, the task still resumes correctly, but the most recent completed step may show missing or undercounted cost in Phase 1.

#### Zombie Worker Protection in the Checkpointer

The `PostgresDurableCheckpointer` is constructed with the current `worker_id` and `task_id` at the start of each task execution. Its `put()` method joins against the `tasks` table to verify `lease_owner = :worker_id AND status = 'running'` before writing. If the lease has been revoked (heartbeat detected it, or reaper reclaimed), the write fails with 0 rows affected, and the checkpointer raises a `LeaseRevokedException` that the graph executor catches to stop execution immediately. This prevents a zombie worker from writing stale checkpoints after its lease expires.

### 5.5 Error Handling, Timeouts & Dead Letter

> **Covers:** F8, F9, F10, R2, R3, R4, R5

#### Retry Model

**Retry is per-task, not per-node.** When a node fails with a retryable error, the entire task is re-queued (after backoff). The new Worker Service instance that claims it resumes from the last checkpoint.

**Backoff enforcement:** The `retry_after` column on the tasks table enforces backoff delays. When a task is re-queued, `retry_after` is set to `NOW() + backoff_interval`. The claim query includes `AND (retry_after IS NULL OR retry_after < NOW())`, so the task is invisible to workers until the backoff expires.

**Backoff schedule:** Exponential — 1s, 2s, 4s (capped at 3 retries by default). Formula: `2^retry_count` seconds.

**Error classification:**

The Worker Service wraps `graph.astream()` in a try/except block. LangGraph surfaces errors from LLM calls and tool calls as exceptions. The runtime classifies these exceptions and decides the task's fate:

| Error | Retryable? | Action |
|-------|-----------|--------|
| LLM API 5xx | Yes | Re-queue task with backoff |
| LLM API timeout | Yes | Re-queue task with backoff |
| LLM API 4xx (bad request) | No | Dead letter |
| LLM API 429 (rate limit) | Yes | Re-queue task with backoff |
| Tool execution error (transient) | Yes | Re-queue task with backoff |
| Tool not in `allowed_tools` | No | Dead letter (tool permission enforced by LangGraph `ToolNode` wrapper) |
| Tool argument validation failure | No | Dead letter (schema validation enforced by LangGraph `ToolNode` wrapper) |
| Worker OOM / crash | Yes | Lease expires -> reaper reclaims -> re-queue with backoff |
| `GraphRecursionError` (max steps) | No | Dead letter |
| Task timeout exceeded | No | Dead letter |
| `LeaseRevokedException` | — | Worker stops; reaper handles re-queue or dead letter |

#### Timeout Hierarchy

```
Task timeout:  Max total wall-clock time for a task
               Default: 3600s (1 hour)
               Enforced by: reaper scans for tasks where created_at + task_timeout_seconds < NOW()
               and transitions them directly to dead_letter

Max steps:     Circuit breaker for infinite node loops (LangGraph recursion_limit)
               Default: 100 (maps to LangGraph's recursion_limit config parameter)
               Enforced by: LangGraph raises GraphRecursionError when the limit is hit.
               The Worker Service catches this and dead-letters the task.
               NOTE: recursion_limit counts graph super-steps (node executions), not
               "logical steps." An LLM call -> tool call -> LLM response cycle is
               ~3 super-steps. Set max_steps accordingly (e.g., 100 super-steps ≈ 33 turns).
```

#### Dead Letter

**Entry conditions:**
- `retry_count >= max_retries` (default 3)
- `task_age > task_timeout_seconds` (enforced by reaper)
- `GraphRecursionError` — LangGraph hit `recursion_limit` (maps to `max_steps`)
- Manual cancellation via `POST /v1/tasks/{task_id}/cancel`
- Non-retryable error (4xx from LLM API, invalid tool definition)

**Dead letter record preserves:** Full checkpoint history (graph states and metadata), final error code/message, last worker ID, dead-letter reason/time, retry-attempt timestamps, agent config snapshot.

**Redrive:** `POST /v1/tasks/{task_id}/redrive` resets `retry_count = 0` and sets `status = queued`. The task resumes from the last checkpoint — completed super-steps are not re-executed. Checkpoint rollback is deferred to Phase 2 (see Section 3).

**Phase 1 error field semantics:** A task that eventually succeeds should not continue to surface stale retry errors as if it were unhealthy. `last_error_code` and `last_error_message` are therefore cleared on successful completion. Rich append-only retry/error audit history is deferred to Phase 2+.

### 5.6 Security Overview

> **Covers:** S5, S6

Tool execution security is enforced at graph execution time, not just at submission:

- **Secret isolation:** In Phase 1, API keys for LLM providers and tool backends are loaded from environment variables at execution time. Integrating AWS Secrets Manager is deferred to Phase 2+ as deployment hardening. Secrets must never be inserted into LangGraph state, checkpoint payloads, metadata, or logs. The checkpointer does not mutate persisted state; log redaction happens in the logging layer, and code review/tests enforce that secrets never enter checkpointed state.
- **Prompt injection mitigation:** Tool call outputs are treated as untrusted data. They are placed in clearly delineated content blocks in the prompt and are never injected into system prompts. This prevents a malicious tool response from hijacking the agent's instructions.

See LLD §6.3 for enforcement details (allowed_tools checks, argument schema validation, tenant scoping).

### 5.7 Observability Strategy

> **Covers:** O1, O2, O3, O4

Every Worker Service instance emits structured logs and metrics to support debugging, alerting, and capacity planning:

- **Structured logging:** Every log line includes `task_id`, `worker_id`, and `node_name` for correlation. Key lifecycle events (task claimed, node started/completed, checkpoint saved, graph resumed, lease revoked, task completed/dead-lettered) are logged as named events.
- **Metrics:** Counters and gauges for queue depth, active tasks, node latency, cost, lease expiry rate, and empty poll frequency. Emitted via OpenTelemetry and exported to CloudWatch in Phase 1.
- **Alerts:** Fire on dead letter accumulation, lease expiry spikes, worker saturation, and task age outliers. Thresholds are tuned to catch systemic issues (not individual task failures).

See LLD §6.4 for the full metrics catalog, event list, and alert thresholds.

---

## 6. Low-Level Design

### 6.1 Database Schema & Key Queries

> **Covers:** F1, F3, F5, F6, F8, F9, F10, R1, R2, R3, R4, R5, R7, S7

```sql
-- Tasks table (also serves as the queue in Phase 1)
CREATE TABLE tasks (
    task_id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           TEXT NOT NULL DEFAULT 'default',
    agent_id            TEXT NOT NULL,
    agent_config_snapshot JSONB NOT NULL,
    status              TEXT NOT NULL DEFAULT 'queued'
                        CHECK (status IN ('queued','running','completed','dead_letter')),
    worker_pool_id      TEXT NOT NULL DEFAULT 'shared',
    version             INT NOT NULL DEFAULT 1,
    input               TEXT NOT NULL,
    output              JSONB,
    lease_owner         TEXT,
    lease_expiry        TIMESTAMPTZ,
    retry_count         INT NOT NULL DEFAULT 0,
    max_retries         INT NOT NULL DEFAULT 3,
    retry_after         TIMESTAMPTZ,
    retry_history       JSONB NOT NULL DEFAULT '[]'::jsonb,
    task_timeout_seconds INT NOT NULL DEFAULT 3600,
    max_steps           INT NOT NULL DEFAULT 100,
    last_error_code     TEXT,
    last_error_message  TEXT,
    last_worker_id      TEXT,
    dead_letter_reason  TEXT
                        CHECK (dead_letter_reason IS NULL OR dead_letter_reason IN
                            ('cancelled_by_user','retries_exhausted','task_timeout','non_retryable_error','max_steps_exceeded')),
    dead_lettered_at    TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Worker polling: claim oldest queued task in target pool, respecting retry backoff
CREATE INDEX idx_tasks_claim ON tasks (worker_pool_id, created_at)
    WHERE status = 'queued';

-- Reaper: find tasks with expired leases
CREATE INDEX idx_tasks_lease_expiry ON tasks (lease_expiry)
    WHERE status = 'running' AND lease_expiry IS NOT NULL;

-- Reaper: find tasks that exceeded total timeout
CREATE INDEX idx_tasks_timeout ON tasks (created_at)
    WHERE status IN ('running', 'queued');

-- Lookup by agent (scoped by tenant)
CREATE INDEX idx_tasks_tenant_agent ON tasks (tenant_id, agent_id, created_at);

-- Dead-letter API: newest failures first, optionally filtered by agent
CREATE INDEX idx_tasks_dead_letter ON tasks (tenant_id, agent_id, dead_lettered_at DESC, task_id DESC)
    WHERE status = 'dead_letter';

-- Checkpoints table (acts as LangGraph BaseCheckpointSaver)
-- LangGraph metadata columns are stored as returned by the pinned library version
-- (thread_ts is TEXT, not TIMESTAMPTZ). Phase 1 supports only the root namespace.
CREATE TABLE checkpoints (
    task_id             UUID NOT NULL REFERENCES tasks(task_id),
    checkpoint_ns       TEXT NOT NULL DEFAULT '',
    checkpoint_id       TEXT NOT NULL,
    worker_id           TEXT NOT NULL,
    parent_checkpoint_id TEXT,
    thread_ts           TEXT NOT NULL,  -- LangGraph version string, NOT a native timestamp
    parent_ts           TEXT,           -- Previous checkpoint version string
    checkpoint_payload  JSONB NOT NULL, -- Serialized LangGraph Checkpoint (channel_values, channel_versions, etc.)
    metadata_payload    JSONB NOT NULL DEFAULT '{}'::jsonb, -- LangGraph CheckpointMetadata
    cost_microdollars   INT NOT NULL DEFAULT 0, -- Populated by cost-tracking callback after super-step
    execution_metadata  JSONB, -- Populated by cost-tracking callback (latency_ms, token_counts, model)
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    PRIMARY KEY (task_id, checkpoint_ns, checkpoint_id)
);

CREATE INDEX idx_checkpoints_task_ts ON checkpoints(task_id, thread_ts);
CREATE INDEX idx_checkpoints_task_created ON checkpoints(task_id, checkpoint_ns, created_at);

-- Checkpoint writes table (stores pending writes within a super-step)
-- Required by LangGraph's BaseCheckpointSaver.put_writes() contract and persisted
-- alongside checkpoints for library-managed recovery behavior.
-- No FK to checkpoints: LangGraph calls aput_writes() before aput(), so the
-- checkpoint row does not exist yet when writes are inserted. This matches the
-- upstream langgraph-checkpoint-postgres schema.
CREATE TABLE checkpoint_writes (
    task_id             UUID NOT NULL REFERENCES tasks(task_id),
    checkpoint_ns       TEXT NOT NULL DEFAULT '',
    checkpoint_id       TEXT NOT NULL,
    task_path           TEXT NOT NULL DEFAULT '',
    idx                 INT NOT NULL,
    channel             TEXT NOT NULL,
    type                TEXT,
    blob                BYTEA NOT NULL,

    PRIMARY KEY (task_id, checkpoint_ns, checkpoint_id, task_path, idx)
);
```

#### Key Query Patterns

**Claim a task (database-as-queue):**
```sql
WITH claimable AS (
    SELECT task_id
    FROM tasks
    WHERE status = 'queued'
      AND worker_pool_id = :pool_id
      AND tenant_id = :tenant_id
      AND (retry_after IS NULL OR retry_after < NOW())
    ORDER BY created_at
    LIMIT 1
    FOR UPDATE SKIP LOCKED
)
UPDATE tasks t
SET status = 'running',
    lease_owner = :worker_id,
    lease_expiry = NOW() + INTERVAL '60 seconds',
    version = t.version + 1,
    updated_at = NOW()
FROM claimable c
WHERE t.task_id = c.task_id
RETURNING t.*;
```

Note: The `version` check is intentionally omitted from the WHERE clause — `FOR UPDATE SKIP LOCKED` already guarantees that only one Worker Service instance can claim a given task.

**Heartbeat:**
```sql
UPDATE tasks
SET lease_expiry = NOW() + INTERVAL '60 seconds',
    updated_at = NOW()
WHERE task_id = :task_id
  AND tenant_id = :tenant_id
  AND lease_owner = :worker_id
  AND status = 'running';
```

Note: The heartbeat checks `lease_owner` and `status` only — **not** `version`. `tasks.version` changes on task lifecycle transitions, so checking version here would cause false lease-revocation signals after claim/retry/cancel/reaper transitions while adding no extra safety over lease ownership.

**Checkpointer `put()` — lease-aware checkpoint write:**
```sql
-- The PostgresDurableCheckpointer.put() implementation.
-- Joins against tasks to prevent a zombie worker from writing after lease revocation.
-- Checkpoint writes do not mutate the task row; `tasks.version` changes only on task
-- lifecycle transitions (claim, retry, completion, dead-letter, cancel, redrive).
-- If 0 rows are inserted, the checkpointer raises LeaseRevokedException.
INSERT INTO checkpoints (task_id, checkpoint_ns, checkpoint_id, worker_id, parent_checkpoint_id,
                         thread_ts, parent_ts, checkpoint_payload, metadata_payload)
SELECT :task_id, :checkpoint_ns, :checkpoint_id, :worker_id, :parent_checkpoint_id,
       :thread_ts, :parent_ts, :checkpoint_payload, :metadata_payload
FROM tasks t
WHERE t.task_id = :task_id
  AND t.tenant_id = :tenant_id
  AND t.status = 'running'
  AND t.lease_owner = :worker_id
ON CONFLICT (task_id, checkpoint_ns, checkpoint_id) DO UPDATE
SET checkpoint_payload = EXCLUDED.checkpoint_payload,
    metadata_payload = EXCLUDED.metadata_payload,
    worker_id = EXCLUDED.worker_id,
    parent_checkpoint_id = EXCLUDED.parent_checkpoint_id,
    thread_ts = EXCLUDED.thread_ts,
    parent_ts = EXCLUDED.parent_ts;
```

**Checkpointer `put_writes()` — store pending writes:**
```sql
INSERT INTO checkpoint_writes (task_id, checkpoint_ns, checkpoint_id, task_path, idx, channel, type, blob)
VALUES (:task_id, :checkpoint_ns, :checkpoint_id, :task_path, :idx, :channel, :type, :blob)
ON CONFLICT (task_id, checkpoint_ns, checkpoint_id, task_path, idx)
DO UPDATE SET channel = EXCLUDED.channel, type = EXCLUDED.type, blob = EXCLUDED.blob;
```

**Post-super-step cost update (executed by Worker Service after each `astream()` yield):**
```sql
UPDATE checkpoints
SET cost_microdollars = :cost,
    execution_metadata = :execution_metadata
WHERE task_id = :task_id
  AND checkpoint_ns = :checkpoint_ns
  AND checkpoint_id = :checkpoint_id;
```

**Task completion (after `astream()` is exhausted):**
```sql
UPDATE tasks
SET status = 'completed',
    output = :final_output,
    lease_owner = NULL,
    lease_expiry = NULL,
    last_error_code = NULL,
    last_error_message = NULL,
    version = version + 1,
    updated_at = NOW()
WHERE task_id = :task_id
  AND tenant_id = :tenant_id
  AND status = 'running'
  AND lease_owner = :worker_id;
```

**Completion check on resume:** When LangGraph is initialized with an existing `thread_id`, it loads the last checkpoint and evaluates whether the graph has reached an end state. If so, `astream()` yields nothing and returns immediately. The Worker Service detects this (zero super-steps yielded) and marks the task `completed`. This handles the crash-between-last-checkpoint-and-task-completion edge case.

**Retryable node failure (worker-classified, re-queue with backoff):**
```sql
WITH requeued AS (
    UPDATE tasks
    SET status = 'queued',
        lease_owner = NULL,
        lease_expiry = NULL,
        retry_count = retry_count + 1,
        retry_after = NOW() + (POWER(2, retry_count) * INTERVAL '1 second'),
        retry_history = retry_history || jsonb_build_array(NOW()),
        last_error_code = :error_code,
        last_error_message = :error_message,
        version = version + 1,
        updated_at = NOW()
    WHERE task_id = :task_id
      AND tenant_id = :tenant_id
      AND status = 'running'
      AND lease_owner = :worker_id
      AND retry_count < max_retries
    RETURNING task_id, worker_pool_id
)
, notified AS (
    SELECT pg_notify('new_task', worker_pool_id)
    FROM requeued
)
SELECT task_id
FROM requeued;
```

**Retryable node failure with retries exhausted (worker-classified terminal failure):**
```sql
UPDATE tasks
SET status = 'dead_letter',
    last_worker_id = :worker_id,
    lease_owner = NULL,
    lease_expiry = NULL,
    last_error_code = :error_code,
    last_error_message = :error_message,
    dead_letter_reason = 'retries_exhausted',
    dead_lettered_at = NOW(),
    version = version + 1,
    updated_at = NOW()
WHERE task_id = :task_id
  AND tenant_id = :tenant_id
  AND status = 'running'
  AND lease_owner = :worker_id
  AND retry_count >= max_retries;
```

**Reaper — lease expiry scan:**
```sql
WITH requeued AS (
    UPDATE tasks
    SET status = 'queued',
        lease_owner = NULL,
        lease_expiry = NULL,
        retry_count = retry_count + 1,
        retry_after = NOW() + (POWER(2, retry_count) * INTERVAL '1 second'),
        retry_history = retry_history || jsonb_build_array(NOW()),
        version = version + 1,
        updated_at = NOW()
    WHERE status = 'running'
      AND lease_expiry < NOW()
      AND retry_count < max_retries
    RETURNING task_id, worker_pool_id
)
, notified AS (
    SELECT pg_notify('new_task', worker_pool_id)
    FROM requeued
)
SELECT task_id
FROM requeued;
```

```sql
UPDATE tasks
SET status = 'dead_letter',
    last_worker_id = lease_owner,
    lease_owner = NULL,
    lease_expiry = NULL,
    last_error_code = 'retries_exhausted',
    last_error_message = 'max retries reached after lease expiry',
    dead_letter_reason = 'retries_exhausted',
    dead_lettered_at = NOW(),
    version = version + 1,
    updated_at = NOW()
WHERE status = 'running'
  AND lease_expiry < NOW()
  AND retry_count >= max_retries
RETURNING task_id;
```

**Reaper — task timeout scan:**
```sql
UPDATE tasks
SET status = 'dead_letter',
    last_worker_id = lease_owner,
    lease_owner = NULL,
    lease_expiry = NULL,
    last_error_code = 'task_timeout',
    last_error_message = 'task exceeded task_timeout_seconds',
    dead_letter_reason = 'task_timeout',
    dead_lettered_at = NOW(),
    version = version + 1,
    updated_at = NOW()
WHERE status IN ('running', 'queued')
  AND created_at + (task_timeout_seconds * INTERVAL '1 second') < NOW()
RETURNING task_id;
```

**Cancel a task:**
```sql
UPDATE tasks
SET status = 'dead_letter',
    last_worker_id = lease_owner,
    lease_owner = NULL,
    lease_expiry = NULL,
    last_error_code = 'cancelled_by_user',
    last_error_message = 'task cancelled by user request',
    dead_letter_reason = 'cancelled_by_user',
    dead_lettered_at = NOW(),
    version = version + 1,
    updated_at = NOW()
WHERE task_id = :task_id
  AND tenant_id = :tenant_id
  AND status IN ('queued', 'running')
RETURNING task_id, status;
```

The worker detects cancellation on the next heartbeat (lease_owner cleared) and stops execution.

**Worker-classified terminal failures (non-retryable error or max steps exceeded):**
```sql
UPDATE tasks
SET status = 'dead_letter',
    last_worker_id = :worker_id,
    lease_owner = NULL,
    lease_expiry = NULL,
    last_error_code = :error_code,
    last_error_message = :error_message,
    dead_letter_reason = :dead_letter_reason, -- 'non_retryable_error' or 'max_steps_exceeded'
    dead_lettered_at = NOW(),
    version = version + 1,
    updated_at = NOW()
WHERE task_id = :task_id
  AND tenant_id = :tenant_id
  AND status = 'running'
  AND lease_owner = :worker_id;
```

**Redrive a dead-lettered task:**
```sql
-- Checkpoint rollback (DELETE latest checkpoint + writes) deferred to Phase 2.
WITH redriven AS (
    UPDATE tasks
    SET status = 'queued',
        retry_count = 0,
        retry_after = NULL,
        lease_owner = NULL,
        lease_expiry = NULL,
        last_error_code = NULL,
        last_error_message = NULL,
        last_worker_id = NULL,
        dead_letter_reason = NULL,
        dead_lettered_at = NULL,
        version = version + 1,
        updated_at = NOW()
    WHERE task_id = :task_id
      AND tenant_id = :tenant_id
      AND status = 'dead_letter'
    RETURNING task_id, worker_pool_id
)
, notified AS (
    SELECT pg_notify('new_task', worker_pool_id)
    FROM redriven
)
SELECT task_id
FROM redriven;
```

The redriven task resumes from the last checkpoint — LangGraph loads the saved state and continues from there.

**Max steps circuit breaker:**

Enforced by LangGraph's `recursion_limit` config parameter, passed when calling `graph.astream()`:

```python
config = {
    "configurable": {"thread_id": str(task_id)},
    "recursion_limit": task.max_steps,
}
async for event in graph.astream(input, config=config):
    # process super-step events
```

When the limit is hit, LangGraph raises `GraphRecursionError`. The Worker Service catches this and transitions the task to `dead_letter` with reason `max_steps_exceeded`. This is enforced *inside* the graph execution — no external polling needed.

### 6.2 Idempotency & Crash Recovery Protocol (LangGraph Checkpointer)

> **Covers:** F5, F6, S1

To guarantee correct crash recovery and minimize duplicate side-effects, the system relies on the LangGraph `BaseCheckpointSaver` interface backed by PostgreSQL, combined with tool-level idempotency annotations.

**The Checkpointer Contract:**
1. After each super-step (node execution), LangGraph calls `checkpointer.put()` with the new graph state.
2. The `PostgresDurableCheckpointer` writes the checkpoint to the `checkpoints` table.
3. During node execution, LangGraph calls `checkpointer.put_writes()` to record pending writes required by the checkpoint saver contract.
4. In Phase 1, these writes are treated as library-internal recovery data. Application-level safety still assumes that an interrupted node may be re-executed in full after crash.

**Crash Recovery:**
1. A new Worker Service instance reclaims the task and calls `graph.astream(thread_id=task_id)`.
2. The `PostgresDurableCheckpointer.get_tuple()` loads the latest checkpoint.
3. LangGraph evaluates the checkpoint state and uses the persisted checkpoint data required by the pinned saver implementation to resume from the correct position.

**Split-brain protection:** The `PRIMARY KEY (task_id, checkpoint_ns, checkpoint_id)` constraint on the checkpoints table, combined with the lease-owner check in `put()`, ensures that if two Worker Services somehow both attempt to write (e.g., a split-brain scenario), only the lease holder succeeds. The other's `INSERT ... SELECT FROM tasks WHERE lease_owner = :worker_id` returns 0 rows, triggering `LeaseRevokedException`.

**Tool idempotency:** In Phase 1, all pre-registered tools (`web_search`, `read_url`, `calculator`) are idempotent, read-only operations served via the co-located MCP server and are safe to re-execute after a crash. The `allowed_tools` whitelist is enforced at task submission — only registered tools are accepted, eliminating the possibility of non-idempotent tool execution. Non-idempotent tool guards (checkpoint-before-call, dead-letter on re-execution) are deferred to Phase 2 when customer-provided mutable tools are introduced via the Custom Tool Runtime.

### 6.3 Security

> **Covers:** S1, S2, S3, S4, S5, S6, S7

#### Data Isolation
- **Tenant-scoped queries:** All API endpoints and queries include `tenant_id`. In Phase 1 this is always `"default"`, but the column exists and is indexed so that adding authentication in Phase 2 does not require a schema migration or query rewrite.
- **Agent-level data isolation:** API endpoints filter by `agent_id` within tenant. No cross-agent data access.

#### Tool Execution Security
- **Scoped tool permissions:** `agent_config_snapshot.allowed_tools` is enforced by a custom `ToolNode` wrapper that checks the allow list before dispatching any tool call to the co-located MCP server. Tools not in the list raise a non-retryable error.
- **Argument validation:** Tool call arguments are validated against the per-tool JSON schema (sourced from the MCP server's tool definitions) by the `ToolNode` wrapper before execution. Unknown or malformed arguments are rejected. This prevents a malicious/confused LLM response from passing dangerous arguments to tools.
- **Tool idempotency (Phase 1 simplification):** All Phase 1 tools are idempotent and read-only by design. The `allowed_tools` list is validated against the co-located MCP server's `listTools` at submission time, so non-idempotent tools cannot enter the system. Non-idempotent tool guards are deferred to Phase 2 (see Section 6.2).

#### Secret Handling
- In Phase 1, API keys for LLM providers and tool backends are stored in environment variables only. Never in checkpoint payloads or agent config.
- Tool handlers receive secrets from process environment at execution time. AWS Secrets Manager integration is deferred to Phase 2+.
- The `PostgresDurableCheckpointer` must preserve exact state fidelity; it does not scrub or rewrite payloads before persistence.

#### Input Validation
- All API inputs are validated against constraints (see Section 3). Reject requests that exceed size limits or contain invalid values.
- `agent_config.allowed_tools` is validated against the co-located MCP server's `listTools` response — arbitrary tool names are rejected.
- `agent_config.model` is validated against supported models — arbitrary model strings are rejected.

#### Prompt Injection Mitigation
- Tool call outputs are treated as untrusted data. Placed in clearly delineated content blocks in the prompt, never injected into system prompts.

#### No Authentication in Phase 1
- The API is internal-only. Authentication/authorization is deferred to Phase 2.
- The `tenant_id` column and tenant-scoped queries are in place so that auth can be added without structural changes.

### 6.4 Observability

> **Covers:** O1, O2, O3, O4

#### Metrics (OpenTelemetry -> CloudWatch in Phase 1)

```
tasks.submitted         -- counter, by agent_id
tasks.completed         -- counter, by agent_id
tasks.dead_letter       -- counter, by agent_id, by error_type
tasks.active            -- gauge, by agent_id
nodes.duration_ms       -- histogram, by node_name
nodes.cost_microdollars -- counter, by agent_id, by model
workers.active_tasks    -- gauge, by worker_id
queue.depth             -- gauge (count of status='queued')
poll.empty              -- counter, by worker_id (empty poll frequency)
leases.expired          -- counter
heartbeats.missed       -- counter, by worker_id
```

#### Logging

Structured logs with `task_id`, `worker_id`, and `node_name` correlation on every log line. Key events logged:

- `TASK_CLAIMED`: Worker Service claimed task, includes retry_count
- `NODE_STARTED`: LangGraph node execution beginning, includes node_name
- `NODE_COMPLETED`: node done, includes node_name, latency_ms, and cost
- `CHECKPOINT_SAVED`: checkpoint written to DB after super-step
- `GRAPH_RESUMED`: task resumed from existing checkpoint on recovery, includes checkpoint_id
- `NODE_REEXECUTED`: node re-executed after crash recovery (no prior checkpoint for this node)
- `LEASE_REVOKED`: heartbeat returned 0 rows, Worker Service stopping execution
- `TASK_COMPLETED`: task finished, includes total checkpoints and total cost
- `TASK_DEAD_LETTERED`: task moved to dead letter, includes reason

Checkpoint provenance is also queryable directly because each checkpoint row stores `worker_id`.

#### Alerts

| Alert | Condition | Severity |
|-------|-----------|----------|
| Dead letter accumulation | `tasks.dead_letter.count > 0` for > 5 min | P2 |
| Lease expiry spike | `leases.expired.rate > 10/min` | P2 |
| Worker saturation | `workers.active_tasks / MAX_CONCURRENT > 0.9` for > 5 min | P3 |
| Task age outlier | Any task in `running` for > `task_timeout_seconds` | P2 |

---

## 7. Demo Scenario

> **Covers:** D1, D2, D3, D4

1. Submit a multi-step research task via `POST /v1/tasks` with `max_steps: 50` (handled by API Service)
2. Worker Service instance A claims the task; LangGraph executes ~5 super-steps (agent node → tools node → agent node → ...)
3. Kill Worker Service instance A mid-execution (simulate crash)
4. Lease expires after 60s, reaper (running in any Worker Service instance) reclaims the task and re-queues it
5. Worker Service instance B claims the task
6. Instance B initializes LangGraph with the same `thread_id` — the `PostgresDurableCheckpointer` loads the last checkpoint, and LangGraph resumes from exactly where it left off
7. Logs: `GRAPH_RESUMED: Loaded checkpoint cp-abc at super-step 5. Skipping 5 completed nodes.`
8. Task completes successfully
9. Query `GET /v1/tasks/{task_id}/checkpoints` (via API Service) — full checkpoint history with timing and cost breakdown; each checkpoint row shows which Worker Service instance produced it via `worker_id`
10. Display cost comparison: "Cost without checkpointing: $0.045 (10 LLM calls). Cost with checkpointing: $0.023 (5 re-used after crash). Savings: 49%."

### Demo Dashboard (stretch goal)

A single-page HTML dashboard that polls `GET /v1/tasks/{id}` and displays:
- Checkpoint timeline with live progress updates
- Per-node cost and latency
- Crash event marker (gap in timeline where lease expired)
- Resume event marker (new Worker Service instance picks up)
- Running cost total vs. estimated cost without checkpointing

This makes the crash-recovery story visually compelling for a demo video, compared to raw API responses.
