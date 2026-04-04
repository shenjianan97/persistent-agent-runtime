# Core Concepts

## Three-Component Model

```
┌─────────────┐       ┌─────────────┐       ┌─────────────┐
│   Agent      │       │    Task      │       │   Worker     │
│  (identity)  │       │   (work)     │       │  (process)   │
│              │       │              │       │              │
│ Lives in DB  │◄──────│ belongs to   │       │ Claims task  │
│ Never dies   │  1:N  │ one agent    │◄──────│ Loads agent  │
│ Has persona  │       │ Has steps    │       │ Executes     │
│ Has memory   │       │ Has state    │       │ Can crash    │
│ Has config   │       │              │       │ Replaceable  │
└─────────────┘       └─────────────┘       └─────────────┘
```

- **Agent:** Identity and configuration stored in the database. An agent is data, not a process—it never "goes down." It defines persona, models, tools, memory, and budgets.
- **Task:** A unit of work belonging to one specific agent. The agent's config dictates how the task is executed.
- **Step:** A logical pausing point within a task (e.g., waiting for an LLM response or external tool execution). The runtime saves the state after every step, enabling resume-from-checkpoint after crashes.
- **Worker:** A stateless process that claims tasks and executes steps. It loads the agent's config to "become" that agent. If a worker crashes, another loading the same config continues seamlessly.

In Phase 1, `agent_id` is a string field on Task with agent config stored inline. Phase 1 supports a single top-level graph only; subgraphs are out of scope. In Phase 2, Agent becomes a first-class entity in the database.

## Key Mechanisms

- **Lease-based ownership** — Workers hold time-bounded leases on tasks. Heartbeats extend leases. Expired leases are reclaimed by a reaper. Prevents both orphaned tasks and dual execution.
- **Checkpoint-resume** (not event-sourced replay) — On crash recovery, find the last completed step and continue from there. No determinism constraints. Chosen because LLM non-determinism makes Temporal-style replay unsuitable.
- **Idempotency via LangGraph checkpointing** — In Phase 1, LangGraph's `BaseCheckpointSaver` backed by PostgreSQL ensures each super-step is checkpointed before the next begins. On crash recovery, previously checkpointed nodes are not re-executed, but an interrupted node may be re-executed in full, so side-effecting tools must be idempotent or explicitly guarded. Transactional outbox is reserved for queue migration in Phase 2 (PostgreSQL -> SQS FIFO).
- **Database-as-queue** (Phase 1) — Tasks are stored and claimed from the same database atomically, eliminating the dual-write problem between a separate queue and state store.
- **Two-level memory** — LangGraph graph-state checkpoints in PostgreSQL natively include conversation history within a task. Long-term memory is distilled knowledge across tasks, stored as append-only entries in S3 with periodic compaction.
- **Error and retry model** — Steps that fail are retried with exponential backoff (1s, 2s, 4s). Non-retryable errors (4xx from LLM API, invalid tool definition) skip retries and fail immediately. Retry is per-task (default max 3 retries), resuming from the last completed checkpoint. Tasks exceeding max retries are moved to dead letter. Budget enforcement is deferred to Phase 2, where budget-exceeded tasks pause rather than fail.

## Developer Experience & Integration

This project leverages **LangGraph** as the underlying execution framework but replaces the infrastructure deployment burden. Developers write standard LangGraph `StateGraph` definitions (nodes and edges). The Worker Service loads your graph and calls `graph.astream()`, while a custom `PostgresDurableCheckpointer` durably syncs the state to your database. The runtime owns the durable execution loop (queuing, leases, retries, and later budgeting) securely wrapping the LangGraph execution.

**Submitting a Task:**
```json
// POST /v1/tasks
{
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

## Architectural Tradeoffs — Positions Taken

| Tradeoff | Decision | Rationale |
|----------|----------|-----------|
| Checkpoint-resume vs event-sourced replay | **Checkpoint-resume** | LLM calls are non-deterministic; replay would produce different results. |
| Strong vs eventual consistency | **Strong consistency on the execution path** | Prevents dual execution after worker crashes. Eventual consistency only for observability reads. |
| Database-as-queue vs separate queue | **Database-as-queue for Phase 1** | Eliminates dual-write problem. PostgreSQL handles 5K-10K claims/sec. |
| Standalone runtime vs Temporal application | **Standalone runtime** | AI-specific problems need control over the execution loop. Better portfolio signal. |
| Tool side-effect containment vs flexibility | **Phase 1: idempotent-only built-in tools via internal MCP. Phase 2: customer-provided tools via Custom Tool Runtime (managed MCP servers).** | Phase 1 pre-registers only read-only idempotent tools served via a co-located MCP server. Phase 2 lets customers upload custom MCP server containers that run in isolated compute within the platform's VPC. |

## Phases

### Phase 1 — Durable Execution MVP

**Goal:** Prove that tasks survive worker crashes and resume correctly from the last checkpoint.

**Scope:** Task submission API, LangGraph execution engine with lease-based ownership, custom `PostgresDurableCheckpointer`, reaper for expired leases, dead letter handling, per-node cost/token tracking via Langfuse, two-layer observability.

### Phase 2 — Multi-Agent & Cost-Aware Scheduling

**Goal:** Support multiple agents with fair scheduling and budget enforcement.

**Scope:** Agent entity, cost-aware scheduler, fair scheduling, worker backpressure, memory compaction, Custom Tool Runtime (BYOT), SQS FIFO migration.

### Future Directions (Post Phase 2)

- Cross-agent coordination (request-response between agents, deadlock detection)
- Agent versioning and rolling updates
- Human-in-the-loop approval workflows
- Execution history replay debugger

## Scaling Outlook

| Scale | Steps/sec | DB ops/sec | First Bottleneck |
|-------|-----------|------------|------------------|
| 1K agents | 40 | 160 | Nothing — system is idle |
| 10K agents | 400 | 1,600 | LLM API rate limits |
| 50K agents | 2,000 | 8,000 | LLM API cost ($72K/hour) |
| 100K agents | 4,000 | 16,000 | Step history storage (500GB/day) |

**Key insight:** The runtime is not the scaling bottleneck — the LLM API (rate limits and cost) is. This validates investing in cost-aware scheduling.

## What This Project Is Not

- Not a chatbot framework — no conversation UI, no streaming responses
- Not a reimplementation of Temporal — it solves AI-agent-specific problems
- Not a prompt engineering playground — the focus is execution infrastructure
- Not a replacement for LangGraph — this project *uses* LangGraph under the hood
- Not an infrastructure management burden — developers define the agent and submit tasks; the runtime handles the rest
