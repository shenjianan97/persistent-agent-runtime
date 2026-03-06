# Design Notes — Phase 2+ Reference Material

**Origin:** Extracted from the former `DESIGN.md` shared design document. These sections contain architectural concepts that are relevant to Phase 2 and beyond, preserved here as reference material for when Phase 2 design work begins.

---

## 1. Agent Entity (Full Definition)

In Phase 1, agent config is snapshotted inline on the Task record. In Phase 2, Agent becomes a first-class entity with its own table, enabling per-agent concurrency limits, fair scheduling, and budget enforcement.

```
agent_id:             string
config:               JSON
  ├── system_prompt:  string ("you are a research assistant that...")
  ├── model:          string ("claude-sonnet-4-6")
  ├── temperature:    float (0.7)
  └── allowed_tools:  []string (["web_search", "read_file"])
memory_ref:           string (S3 prefix for long-term memory, e.g., s3://bucket/memory/agent123/)
max_concurrent_tasks: int (default 5)
task_ttl_seconds:     int (default 86400)
budget_max_per_task:  int (microdollars, default 500000 = $0.50)
budget_max_per_hour:  int (microdollars, default 5000000 = $5.00)
status:               enum (active | paused | disabled)
```

An agent is a database record that defines identity. It stores WHAT the agent is (persona, model, tools, budget) but not WHERE it runs. Any worker can act as any agent by loading this config.

---

## 2. Worker Pools & Private Workers (BYOW)

Tasks specify a `worker_pool_id`. By default, tasks run on the "shared" pool (elastic Fargate containers with public internet access only). Customers can also deploy a Private Worker binary inside their own VPC, associated with a custom `worker_pool_id`. Because workers *pull* tasks from the queue, this allows agents to securely access customer internal databases or Model Context Protocol (MCP) servers without requiring the customer to open inbound firewall ports.

### VPC & MCP Security via Private Workers

Exposing Model Context Protocol (MCP) servers or internal APIs directly to the internet is a severe security risk. The runtime uses a "Bring Your Own Worker" (BYOW) model where the worker runs inside the customer's secure perimeter. The worker connects to local MCP servers via `stdio` or internal networking, and only makes outbound requests to the central runtime to poll for tasks and post results.

---

## 3. Agent Memory Model

### Why memory is an infrastructure problem

LLMs are completely stateless — they retain nothing between API calls. The only way to give an LLM "memory" is to include context in the prompt. Every LLM call the runtime makes is assembled from stored data:

```
┌─────────────────────────────────────────────┐
│ LLM API call = assembled prompt             │
│                                             │
│  1. System prompt (agent config)            │
│  2. Long-term memory (loaded from S3)       │
│  3. Conversation history (prior steps)      │
│  4. Current step input                      │
└─────────────────────────────────────────────┘
```

This means memory is directly a cost and latency problem — every byte of memory stored is a byte that eventually goes into a prompt, which means more tokens billed and slower responses. At the context window limit (~128K-200K tokens), memory physically cannot fit and must be truncated or summarized.

### Two storage levels

**Step checkpoints (per-task) — step records in PostgreSQL**

Every step's input and output is stored as `input_payload` and `output_payload` on the Step record. These serve a dual purpose:
1. **Checkpointing** — On crash recovery, a new worker reads completed steps to know where to resume.
2. **Conversation history** — The same step records are loaded and assembled into the LLM prompt as context for the next step, just like how a chat application sends the full conversation history with every API call.

There is no separate "conversation memory" store. Step records *are* the conversation history. When a task completes, its step records are no longer loaded into future tasks.

The effective size bound for step checkpoints is the model's context window minus the space used by system prompt, long-term memory, and current input. For a 200K token context window with ~50K tokens of long-term memory, roughly 148K tokens remain for conversation history. If the history exceeds the available space, the worker truncates oldest steps first, preserving recent context.

**Long-term memory (per-agent, across tasks) — append-only entries in S3**

Distilled knowledge that persists between tasks: facts learned, user preferences, domain knowledge. This is NOT raw conversation history — it's structured knowledge extracted from completed tasks.

Long-term memory uses an **append-only** model to support concurrent task execution without write conflicts. Each completed task appends a new memory entry as a separate S3 object — no read-modify-write on a shared document.

```
s3://bucket/memory/agent123/
  ├── compacted-002.json   (latest compaction snapshot)
  ├── entry-015.json       (from task after last compaction)
  ├── entry-016.json       (from concurrent task)
  └── entry-017.json       (from another concurrent task)
```

The write flow:
1. Task runs — step records accumulate in PostgreSQL as conversation history
2. Task completes — a post-task extraction step distills key learnings from the conversation
3. Learnings are written as a new memory entry object in S3 (append-only, no conflicts)

The read flow (when a new task starts):
1. Load the latest compacted snapshot (e.g., `compacted-002.json`)
2. Load any entries created after that snapshot (e.g., `entry-015.json` through `entry-017.json`)
3. Concatenate: compacted snapshot + newer entries = full long-term memory context for the prompt

This keeps reads fast — one snapshot plus a small tail of recent entries — while allowing unlimited concurrent writes.

### Memory summary

| Level | Content | Storage | Included in prompt as | Size Bound | Lifecycle |
|-------|---------|---------|----------------------|------------|-----------|
| Step checkpoints | Step inputs/outputs for current task | Step records in PostgreSQL | Conversation history | Bounded by remaining context window (~148K tokens for a 200K model) | Task lifetime; archived after completion |
| Long-term memory | Distilled knowledge across tasks | Append-only entries in S3, periodically compacted | System prompt context | 200KB compacted (~50K tokens) | Persists across tasks |

### Why not just store everything as raw prompt history?

At 10 steps per task with ~5KB per step, one task generates ~50KB of conversation history. After 100 tasks, an agent has 5MB of raw history. Including all of that in every prompt would:
- Cost ~$0.05-$0.50 per LLM call just for the context (at $3-$15 per million input tokens)
- Add 2-5 seconds of latency per call
- Eventually exceed the context window entirely

Long-term memory must be **distilled, not raw**. "The user's production database is PostgreSQL on RDS in us-east-1" is 15 tokens. The conversation where the agent discovered this fact was 2,000 tokens. Storing the fact, not the conversation, is a 100x compression.

### Compaction strategy

Long-term memory is bounded by what fits in the LLM context window. At ~4 characters per token, 200KB of memory = 50K tokens — a reasonable upper bound that leaves room for system prompt, conversation history, and current input.

**Compaction triggers:**
1. **User-initiated** — Agent owner explicitly requests compaction via API (`POST /agents/{agent_id}/compact`). Useful when the agent has accumulated stale or redundant knowledge.
2. **Periodic** — The runtime runs compaction automatically when the agent has no active tasks (all tasks in terminal states) and uncompacted memory exceeds a configurable threshold (default 200KB). This avoids compacting while tasks are still writing new entries.

**Compaction process:** The runtime submits a compaction task: an LLM call that reads all entries since the last compacted snapshot, merges and summarizes them into a new compressed snapshot (`compacted-NNN.json`). The compaction task runs through the runtime itself (it's just a task), so it gets the same durability guarantees. After compaction, the merged entries are archived (moved to an `archived/` prefix) and no longer loaded on read.

---

## 4. Scaling Analysis

### Back-of-Envelope Numbers

**Assumptions:** Each agent has 1 active task, 20% of agents executing at any moment, ~10 steps/task, ~5s per step (LLM latency dominates), ~5KB per checkpoint.

| Scale | Active | Steps/sec | DB ops/sec | LLM calls/sec | First Bottleneck |
|-------|--------|-----------|------------|----------------|------------------|
| 1K agents | 200 | 40 | 160 | 40 | Nothing — system is idle |
| 10K agents | 2,000 | 400 | 1,600 | 400 | **LLM API rate limits** (most providers cap at 100-500 req/sec) |
| 50K agents | 10,000 | 2,000 | 8,000 | 2,000 | **LLM API cost** ($72K/hour at $0.01/call) |
| 100K agents | 20,000 | 4,000 | 16,000 | 4,000 | **Step history storage** (500GB/day) |

**Key insight:** The runtime is not the scaling bottleneck. The LLM API (rate limits and cost) is the binding constraint. This validates the decision to invest in cost-aware scheduling rather than micro-optimizing the runtime.

### Storage Retention

At 10K agents with moderate activity: ~50GB/day of step history. Retention policy: keep full step history for 7 days, archive to S3 after 7 days, delete after 90 days. This keeps the hot store under 350GB.

---

## 5. DynamoDB Single-Table Design

```
PK                          SK                              Entity
AGENT#agent123             METADATA                         Agent config
AGENT#agent123             TASK#task456                     Task record
AGENT#agent123             TASK#task456#STEP#001            Step record
AGENT#agent123             TASK#task456#STEP#002            Step record

GSI1: status (PK) + created_at (SK)     — worker polling for queued tasks
GSI2: lease_expiry (PK)                 — reaper scanning for expired leases
```

---

## 6. Reliability Mechanisms (Phase 2 additions)

- **Cost runaway prevention:** Per-agent budget enforcement. Tasks exceeding budget are paused.
- **Step history bloat prevention:** 7-day retention in hot store, S3 archival, 90-day TTL.
