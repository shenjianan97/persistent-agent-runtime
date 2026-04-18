# Track 5 Design — Agent Memory

## Context

Phase 2's original memory design (see [phase-2/design.md §3](./design.md#3-agent-memory-model)) sketched an S3 append-only store with LLM-driven compaction and auto-loading into every prompt. That sketch was written before Tracks 1–4 shipped, and it was modeled after personal-assistant products (OpenClaw, Honcho) where one human converses with one agent across many channels, and cross-session continuity is the product.

This track reconsiders memory in light of what the platform actually is: a **managed runtime** for agents that run discrete tasks for customers, often in parallel, often with no continuity between successive tasks. The old design's auto-loading, daily files, and implicit promotion logic fit a personal-assistant shape that does not match the managed-runtime shape.

Track 5 rescopes agent memory to the smallest useful primitive for the managed-runtime use case:

1. **Cross-task knowledge capture** — each completed task produces a distilled memory entry the agent can search later.
2. **Explicit retrieval only** — memory is never auto-injected into prompts. Customers or the agent pull in what they need, when they need it.
3. **Customer-visible** — memory entries are queryable, browsable, and deletable via API and Console. Memory is not a platform black box.

Durable rules and customer-authored facts continue to live in the system prompt, not in a second memory-shaped store. Intra-task context management (the tiered compaction described in [issue #50](https://github.com/shenjianan97/persistent-agent-runtime/issues/50)) is a separate concern and is deferred to **Track 7 — Context Window Management**.

## Goals

- Give every completed task a durable, distilled memory entry scoped to its agent.
- Let the agent surface salient observations mid-execution that persist into the final entry.
- Let customers browse and attach specific past memories to a new task at submission.
- Let the agent search past memories via a tool when relevant.
- Let the agent drill down from a memory entry into the underlying task trace on explicit user request.
- Keep memory opt-in per agent; do nothing (and charge nothing) if memory is disabled.
- Ship a storage design that is simple to operate (single Postgres table) and extends naturally to customer-owned storage later.

## Non-Goals

Track 5 does not include:

- auto-loading of memory entries into every task's prompt
- a separate "long-term" tier beyond what the agent's system prompt already provides
- customer-authored memory entries written directly to the store (customers edit the system prompt or attach past entries; they do not write memory rows by hand)
- tiered in-task context compaction (tool-result clearing, arg truncation, mid-task LLM summarization) — these belong to Track 7
- auto-deletion, retention windows, or decay weighting
- cross-agent memory sharing or cross-tenant access
- "dreaming" / periodic consolidation / promotion jobs
- full raw-trace search across past tasks (only per-task drill-down is supported)
- customer-supplied embedding providers or search backends
- schema for future BYO memory backends (interface boundary is acknowledged but not codified in v1)

## Core Decisions

- Memory is **opt-in per agent** via `agent_config.memory.enabled` (default `false`).
- Storage is a single PostgreSQL table `agent_memory_entries`, scoped by `(tenant_id, agent_id)`.
- Search uses **hybrid BM25 + vector** against the same table (Postgres-native `tsvector` + `pgvector` extension). No SQLite, no S3, no external index.
- Every completed task produces **exactly one memory entry per `task_id`**, written atomically at task termination. Follow-up runs and redrives (which reuse the same `task_id`) **overwrite** the prior entry so it always reflects the latest execution state — not additional rows.
- The agent may call `memory_note(text)` any number of times during execution to append observations. The final write merges those observations with a retrospective summary; the summary does **not** overwrite observations.
- Memory write for **successful** tasks is a **hybrid**: the summarizer LLM call lives inside a dedicated LangGraph node (`memory_write`) so LangGraph's checkpointer provides crash-recovery durability for the expensive step; the database commit is owned by the worker's post-astream path, which co-commits the `UPSERT` into `agent_memory_entries` together with `UPDATE tasks SET status='completed'` in a single transaction with lease validation. The graph node populates a `pending_memory` field on state; the worker reads it from the final state and does the DB writes.
- Memory write for **dead-lettered** tasks is a worker-side hook (no LLM call, template-only; observation-less failures and cancellations write nothing). Branching by `(status, dead_letter_reason)`:
  - `status = 'completed'` → graph-node path above.
  - `status = 'dead_letter'` AND `dead_letter_reason = 'cancelled_by_user'` → nothing written. (Cancellation is modeled as dead-letter with this reason — see Phase 2 Track 2.)
  - `status = 'dead_letter'` (all other reasons) AND agent wrote at least one observation → worker-side hook writes a template entry.
  - `status = 'dead_letter'` (all other reasons) AND no observations → nothing written. Trivial failures don't pollute memory.
- HITL pauses and budget pauses are not task completions; the graph never traverses `memory_write → END` while paused. The node only fires on the path the agent itself chose as terminal (no pending tool calls, graph is exiting).
- Memory is never auto-injected into a task's prompt. Retrieval is explicit only:
  - **Customer attaches** specific entries at task submission
  - **Agent calls** `memory_search` tool during execution
- **`memory_search` is the only cross-task retrieval tool.** Raw-trace access is drill-down-only via `task_history_get(task_id)`, which returns a bounded structured view of one task.
- Summarization LLM cost is recorded in `agent_cost_ledger` for visibility but is **exempt from Track 3 per-task budget enforcement**. Rationale: the memory write is a platform-directed closure step, not agent-directed reasoning — allowing it to trip a per-task budget pause would leave tasks in `paused` state with an `outcome='succeeded'` memory entry, which is incoherent. The call still uses a configurable `summarizer_model` (falls back to a platform default). `budget_max_per_hour` accounting still receives the cost — only the synchronous per-task pause check is skipped.
- Access is scoped to the customer that owns the agent. Cross-tenant access is forbidden at the API layer.
- Retention in v1 is **effectively unbounded for everyday use**, with a platform-level soft cap of **10,000 entries per agent**. When an agent is at the cap and an UPSERT would insert a new row (i.e., the `ON CONFLICT (task_id)` branch does **not** fire):
  - The insert succeeds, and the oldest entry by `created_at` is hard-deleted in the same transaction (FIFO trim).
  - Follow-up and redrive writes that overwrite an existing row (`ON CONFLICT DO UPDATE`) **do not** trigger trim — row count is unchanged, so nothing gets evicted.
  - The Console surfaces a warning on the Memory tab when the agent crosses 80% of the cap.
  - Customers can raise or lower the cap per agent via `agent_config.memory.max_entries` (bounded to a platform-max, e.g., 100,000).
  - This is explicitly a *soft cap to keep migration manageable*, not a retention policy. Customers who need aggressive deletion still do so via the explicit `DELETE` endpoint.

## Data Model

### New table: `agent_memory_entries`

| Column | Type | Constraints / Meaning |
|--------|------|------------------------|
| `memory_id` | `UUID` | Primary key, default `gen_random_uuid()` |
| `tenant_id` | `TEXT` | NOT NULL |
| `agent_id` | `TEXT` | NOT NULL |
| `task_id` | `UUID` | NOT NULL, UNIQUE, soft reference to `tasks(task_id)` — no FK constraint is enforced so a later task-prune does not cascade or orphan memory |
| `title` | `TEXT` | NOT NULL, max 200 chars, human-facing label for Console pickers |
| `summary` | `TEXT` | NOT NULL, retrospective distillation written by the final memory-write node (max ~4KB) |
| `observations` | `TEXT[]` | NOT NULL, default `'{}'`, agent-written mid-task notes in order |
| `outcome` | `TEXT` | NOT NULL, `'succeeded'` or `'failed'` |
| `tags` | `TEXT[]` | NOT NULL, default `'{}'`, optional entity/keyword hints for search precision |
| `content_tsv` | `tsvector` | Generated column (see DDL below), GIN-indexed |
| `content_vec` | `vector(1536)` | Embedding of the same concatenated text, HNSW-indexed. Dimension matches platform default embedding model. Nullable — see [§ Embeddings](#embeddings) for the deferred-embedding fallback. |
| `summarizer_model_id` | `TEXT` | Nullable. Records which model generated the summary. **Not a foreign key** to `models` — the column also stores sentinels `'template:fallback'` (summarizer-outage fallback) and `'template:dead_letter'` (dead-letter path), which would violate an FK. |
| `version` | `INT` | NOT NULL, default `1`. Incremented on each upsert (follow-up, redrive, summary regeneration). Matches the optimistic-concurrency pattern on `tasks.version`. |
| `created_at` | `TIMESTAMPTZ` | NOT NULL, defaults to `now()`, immutable once set |
| `updated_at` | `TIMESTAMPTZ` | NOT NULL, defaults to `now()`, rewritten on each upsert (follow-up / redrive / regeneration) |

**Generated column DDL** (the expression must be IMMUTABLE for `STORED GENERATED`; the `regconfig` two-arg form of `to_tsvector` is required):

```sql
content_tsv tsvector GENERATED ALWAYS AS (
  to_tsvector(
    'english'::regconfig,
    coalesce(title, '') || ' ' ||
    coalesce(summary, '') || ' ' ||
    array_to_string(observations, ' ') || ' ' ||
    array_to_string(tags, ' ')
  )
) STORED
```

Using `to_tsvector('english'::regconfig, …)` (not the single-argument form) is required because the single-argument variant is only `STABLE` since Postgres 12, not `IMMUTABLE`, and cannot back a stored generated column.

**Primary key:** `memory_id`.

**Foreign keys:**
- `(tenant_id, agent_id)` → `agents(tenant_id, agent_id)` — enforces agent ownership and tenant isolation.
- `task_id` — soft reference only, no database-level FK constraint. The task record is the source of provenance; if it is ever pruned, the memory entry remains valid and the linked-task UI simply shows the id without a working link.

**Indexes:**
- `(tenant_id, agent_id, created_at DESC)` — list view.
- GIN on `content_tsv` — BM25 search.
- HNSW on `content_vec` — vector search.
- `UNIQUE (task_id)` — one entry per task.

**Why single table, shared across tenants:** Matches the existing pattern used by `tasks`, `agents`, and `task_events`. Multi-tenant isolation is enforced by `tenant_id` in the API layer and by scoped query predicates, not by physical table partitioning. Per-tenant tables add migration and operational cost without improving isolation.

**Why not SQLite per agent (OpenClaw-style):** OpenClaw is a desktop application with a local filesystem and a single user per agent. The platform's workers are stateless, ephemeral, and horizontally scaled. Per-agent SQLite would require a shared filesystem mount or object-store-backed SQLite — which is effectively reinventing Postgres with worse tooling.

**Why not S3 append-only (the original design):** The original design was optimized for concurrent-write safety. One-entry-per-task is a very low write rate — concurrency is not a problem. S3 also has no usable full-text or vector search, which this track needs as a first-class capability.

### Agent table extension

Track 5 extends `agents.agent_config` with a `memory` section. No new columns on `agents` — the JSON shape accommodates the new keys.

```
agent_config:
  system_prompt: string
  provider: string
  model: string
  temperature: float
  allowed_tools: [string]
  memory:
    enabled: bool                # default: false
    summarizer_model: string     # optional; falls back to platform default (e.g., cheap Haiku-class)
```

When `memory.enabled` is `false` or the `memory` section is absent:

- `memory_note` and `memory_search` tools are not registered for the agent's tasks
- The final memory-write node is skipped
- No memory entries are ever written for the agent
- The agent's memory store remains browsable and deletable via API (historical entries from when it was enabled are preserved)

### New table: `task_attached_memories`

Track 5 adds a dedicated join table for task-to-memory attachments rather than an array column on `tasks`. This mirrors the existing `task_artifacts` / `task_events` relational pattern in the codebase and leaves room for per-attachment metadata later (attach time, reason, injection mode) without requiring a schema migration.

| Column | Type | Constraints / Meaning |
|--------|------|------------------------|
| `task_id` | `UUID` | NOT NULL, FK to `tasks(task_id)` with `ON DELETE CASCADE` |
| `memory_id` | `UUID` | NOT NULL, soft reference to `agent_memory_entries(memory_id)` — no database FK so a later delete of a memory entry does not cascade-delete the attachment audit row |
| `position` | `INT` | NOT NULL, preserves attach order for injection into the prompt context |
| `created_at` | `TIMESTAMPTZ` | NOT NULL, defaults to `now()` — records when the attachment was captured (equals task submit time in v1) |

**Primary key:** `(task_id, memory_id)`.

**Indexes:**
- Primary key already covers `(task_id, memory_id)` for forward lookup and uniqueness.
- `(memory_id)` — for reverse lookup ("which tasks used this memory entry?") to support operator analytics and future reference-count rendering.

**Semantics:**
- Rows are inserted at task submission time. Each row is validated at insertion to belong to the submitting `(tenant_id, agent_id)`.
- Attachments are **immutable after task creation** — not rewritten on follow-up or redrive. The follow-up continues the conversation already seeded with the attached memories.
- When a memory entry is hard-deleted via `DELETE /v1/agents/{agent_id}/memory/{memory_id}`, matching `task_attached_memories` rows **remain** (no FK cascade). They represent the historical fact that the attachment happened, even if the entry itself no longer exists.
- When a task is deleted (unusual — Phase 2 keeps tasks indefinitely), `ON DELETE CASCADE` drops the attachment rows with it.

**Event-trail duplication.** The `task_submitted` event's `details` JSONB also includes the `memory_id` list. This is a deliberate small duplication: event consumers can reconstruct attachment history without joining the join table, and the join table gives constant-time forward and reverse lookup. Both sources should agree; if they diverge, the join table is authoritative (events are append-only and may lag).

**API rendering.** Task detail API responses include:
- `attached_memory_ids: [uuid]` — resolved by querying `task_attached_memories` for this task, ordered by `position`.
- `attached_memories_preview: [{memory_id, title}]` — a join against the current `agent_memory_entries` for human-friendly rendering. The preview applies the same `(tenant_id, agent_id)` scope filter as any single-entry lookup; memory_ids that no longer resolve (deleted entries) are omitted from `attached_memories_preview` but remain present in `attached_memory_ids`.

### pgvector extension

The `pgvector` extension must be enabled on the Postgres database. Embedding dimension is fixed at **1536** in v1, matching the default embedding model (`text-embedding-3-small` or equivalent; see [§ Embeddings](#embeddings)).

## Write Path

### Graph state extension

The `memory_note` tool and the `memory_write` node share two state fields. `MessagesState` does not have these keys natively, so the worker registers a custom state schema for memory-enabled tasks:

```python
class MemoryEnabledState(MessagesState):
    observations: Annotated[list[str], operator.add]
    pending_memory: dict | None
```

- `observations` — append-only list of agent-written notes. The `operator.add` reducer makes appends associative and compatible with LangGraph's super-step merge semantics. `memory_note` returns a state update (either via `Command(update={"observations": [note]})` or the tool-returns-state pattern — pick whichever the implementation prefers); a plain string return is insufficient because LangGraph cannot infer state mutations from scalar returns.
- `pending_memory` — populated once by the `memory_write` node at task completion; read once by the worker's post-astream commit path. Not written by the agent, not reducer-merged. Stays `None` for the entire execution until the final node sets it.

When `memory.enabled = false` the worker uses the default `MessagesState` (no custom schema, no `memory_note` tool, no `memory_write` node in the graph).

### Mid-task writes (optional)

The agent can call `memory_note(text: string)` during task execution. Each call appends one string to the `observations` list in graph state. The tool:

- returns immediately (no LLM call, no I/O beyond graph-state mutation)
- rejects empty strings and strings longer than a per-note cap (2 KB)
- is registered only when `memory.enabled` is `true` for the task's agent

Observations are persisted as part of LangGraph super-step checkpoints (the worker runs with `durability="sync"`). Observations present at the time of the most recent checkpoint survive worker crashes and are available when the task next runs. Observations appended mid-step and not yet checkpointed are lost on crash along with any other in-flight tool results from that super-step; the raw trace for recently-attempted steps remains available via `task_history_get`.

**Follow-up seeding.** When Track 4's follow-up endpoint resumes a previously-completed task, the worker seeds the fresh graph state's `observations` list from the existing memory row's `observations` column (via `agent_memory_entries.observations` lookup by `task_id`) before the graph begins executing. This preserves first-execution observations through follow-up and matches the "entry reflects the combined execution" invariant. Redrive behaves the same way.

### Successful-task memory write — hybrid graph-node + worker commit

Memory write for successful tasks is split between a LangGraph node (for the expensive, durability-sensitive LLM call) and the worker (for the co-commit with task status).

**Graph topology change** (for agents with `memory.enabled = true`):

```
agent ──[pending tool calls?]──┬──► tools ──► agent
                               │
                               └──► memory_write ──► END
```

The existing `tools_condition` edge (`agent → tools | END`) is redirected so the "no pending tool calls" branch goes to `memory_write` instead of `END`. Agents with memory disabled keep the original edge (`agent → END`).

**`memory_write` node responsibilities:**

1. Read `messages` and `observations` from state.
2. Call the `summarizer_model` to produce a `title` (≤10 words, action-oriented) and a `summary` (≤400 words, retrospective), passing observations so the summary complements rather than duplicates them.
3. Compute the embedding over `title + summary + observations + tags`.
4. Return a state update populating a `pending_memory` field:
   ```python
   Command(update={
       "pending_memory": {
           "title": ..., "summary": ..., "outcome": "succeeded",
           "content_vec": ..., "summarizer_model_id": "<model>",
           "observations_snapshot": [...],
       }
   })
   ```
5. **No DB writes from the node itself.** LangGraph's checkpointer persists the state update (including `pending_memory`) via the normal super-step commit.

Running the summarizer call inside a graph node means a mid-call crash is recoverable: the checkpoint does not advance, the reaper re-claims the task, and LangGraph resumes from the prior checkpoint — which re-enters `memory_write` and retries the LLM call. Upsert downstream (step below) absorbs any duplicate-write race.

**Worker post-astream commit:**

1. Graph returns from `astream` normally with `pending_memory` in the final state (confirmable by inspecting the final checkpoint via `aget_tuple`).
2. Worker opens a single Postgres transaction:
   - `UPSERT` into `agent_memory_entries` using fields from `pending_memory` plus `observations_snapshot`.
   - `UPDATE tasks SET status='completed'` — the same UPDATE that already concludes successful tasks today.
   - Both guarded by lease validation (`WHERE lease_owner = :me`).
3. Commit. The task is `completed` and the memory row is visible atomically.

**Failure handling along this path:**

| Crash point | Recovery |
|---|---|
| Mid-summarizer LLM call | Last checkpoint lacks `pending_memory`; reaper re-claim resumes graph, `memory_write` retries. |
| After `memory_write` state update but before worker commit | Checkpoint has `pending_memory`; reaper re-claim; worker reads final state, re-runs the tx. Upsert absorbs any double-write. |
| Lease lost during worker commit | Transaction rolls back (lease predicate fails); reaper eventually re-claims; worker retries from final state. |
| Summarizer unavailable after internal retries | See fallback below. |

**Summarizer-unavailable fallback.** If the `summarizer_model` call fails after its internal retries — provider outage, rate limit, credential rotation lag — the `memory_write` node does **not** skip the write. Instead it populates `pending_memory` from a template:

- `title`: `"Completed: <first 80 chars of task input, sanitized>"`
- `summary`: `"<final_output truncated to ~1KB> [summary generation unavailable; review observations and linked task trace for detail.]"`
- `observations_snapshot`: preserved verbatim
- `outcome`: `'succeeded'` (the task did succeed — only the summary is degraded)
- `summarizer_model_id`: `'template:fallback'`

The failure is recorded in a structured log line. The embedding step still runs on the template text; if the embedding provider is also down, the row is written with `content_vec = NULL` as described in [§ Embeddings](#embeddings). The invariant "every completed memory-enabled task produces exactly one memory entry" is preserved across both normal and degraded paths.

Customers can later regenerate a proper summary for `'template:fallback'` entries once the provider recovers. A regeneration endpoint is **out of scope for v1**.

**Budget interaction.** The summarizer LLM cost is recorded in `agent_cost_ledger` for visibility but is exempt from `budget_max_per_task` enforcement (see [§ Core Decisions](#core-decisions)). Hourly spend (`budget_max_per_hour`) accounting still applies, evaluated on the next task claim. The budget-enforcement path the worker runs between super-steps must skip the pause check when the super-step that just finished was `memory_write`; otherwise a memory-write cost that crosses the per-task cap would leave the task in `paused` with a `pending_memory` populated but no row written. This carve-out is narrow and named by graph-node identity.

### Dead-letter hook (failed tasks with observations)

Cancellation is modeled in Phase 2 Track 2 as `status = 'dead_letter'` with `dead_letter_reason = 'cancelled_by_user'` — there is no distinct `cancelled` status. The dead-letter hook branches accordingly:

1. Worker reaches the dead-letter finalization path (max retries exhausted, or cancel signal observed).
2. If `dead_letter_reason = 'cancelled_by_user'` → **skip entirely**, no memory entry is written. Rationale: the customer explicitly aborted — residue in future contexts would be unwelcome. Observations remain in the checkpoint state for forensic access via `task_history_get`, but are never exfiltrated into the memory table.
3. Otherwise (genuine failure path):
   - Read the task's last checkpoint via `aget_tuple(thread_id)` to recover the `observations` list.
   - If `observations` is empty → skip, no memory entry is written. Trivial failures don't pollute memory.
   - If `observations` is non-empty:
     - Auto-generate a minimal summary: `"Task dead-lettered after <N> retries: <last_error_code> — <last_error_message>"` (template; no LLM call).
     - Generate a short title: `"[Failed] <first_50_chars_of_task_input>"`.
     - Upsert a row with `outcome = 'failed'`, observations preserved, `summarizer_model_id = 'template:dead_letter'`.
     - Compute embedding over the concatenated text.

**Ordering (critical).** The hook must execute in this sequence inside a single transaction with lease validation:

```
BEGIN;
  -- 1. Read last checkpoint (via checkpointer cursor)
  -- 2. Upsert agent_memory_entries row (if observations non-empty + not cancelled)
  -- 3. UPDATE tasks SET status = 'dead_letter', dead_letter_reason = ... WHERE task_id = :id AND lease_owner = :me;
COMMIT;
```

If step 3 fails lease validation, the whole transaction rolls back — no orphan memory row. If the worker crashes between steps 2 and 3, the reaper re-claims the task, the retry path returns through dead-letter, and upsert `ON CONFLICT DO UPDATE` re-writes the same row idempotently.

No LLM is called on the dead-letter path. Cost is effectively zero beyond the embedding call.

### Concurrency and idempotency

- `UNIQUE (task_id)` on `agent_memory_entries` ensures at most one entry per task at any time.
- Memory writes use **upsert**, not insert-or-skip. `created_at` is intentionally absent from the `UPDATE SET` clause so it stays immutable across follow-up / redrive:
  ```sql
  INSERT INTO agent_memory_entries (...) VALUES (...)
  ON CONFLICT (task_id) DO UPDATE SET
    title               = EXCLUDED.title,
    summary             = EXCLUDED.summary,
    observations        = EXCLUDED.observations,
    outcome             = EXCLUDED.outcome,
    tags                = EXCLUDED.tags,
    content_vec         = EXCLUDED.content_vec,
    summarizer_model_id = EXCLUDED.summarizer_model_id,
    version             = agent_memory_entries.version + 1,
    updated_at          = now();
    -- created_at intentionally not updated
  ```
- **Why upsert, not insert-once:** Track 4's follow-up endpoint (`POST /v1/tasks/{task_id}/follow-up`) resumes a completed task under the same `task_id`. A dead-letter-then-redrive sequence also reuses the same `task_id`. In both cases the memory entry must reflect the latest execution, not be permanently frozen to the first attempt's outcome. An insert-once model would leave stale or failed entries in place forever after follow-up or successful redrive.
- **Duplicate-write within a single execution** (e.g., the final node is re-entered after a transient crash before it committed): upsert is still safe. The second call overwrites the first with the same data; `created_at` is preserved, `updated_at` advances.
- **Ordering guarantee:** the final memory-write node (or the dead-letter hook) runs once per execution attempt, so there is no intra-attempt race. Cross-execution upserts are inherently serialized by the task's own lifecycle — only one worker holds a lease at a time.
- **`created_at` vs `updated_at`:** `created_at` is the first time a memory row was written for this `task_id`. `updated_at` is the most recent execution's write time. The Console renders both.

## Read Path

### Retrieval is always explicit

Memory entries are never auto-loaded into a task's prompt. There are exactly three ways an entry is surfaced:

1. **Customer attach at submission** — `POST /v1/tasks` accepts `attached_memory_ids: [uuid]`. The API resolves each id against the submitting tenant+agent in a single `WHERE memory_id = ANY(:ids) AND tenant_id = :caller AND agent_id = :path_agent` query, fails fast on any id that fails to resolve, and copies the resolved `title + observations + summary` blocks into the task's initial prompt context (system-prompt-prefix). Validated ids are then persisted to `task_attached_memories` (see [§ New table: `task_attached_memories`](#new-table-task_attached_memories)) and echoed into the `task_submitted` event's `details` JSONB. Error responses for resolution failures do not distinguish "not found" / "wrong tenant" / "wrong agent" — all three return the same 4xx shape, matching the 404-not-403 rule used elsewhere in the API.
2. **Agent `memory_search` tool call during execution** — see [§ Agent Tools](#agent-tools).
3. **Console browse / attach** — a human user reads entries in the Console, selecting ids to attach at submission time. Same underlying API as (1).

### Search API

`GET /v1/agents/{agent_id}/memory/search` with query parameters:

- `q` (required) — query string
- `limit` (optional, default 5, max 20)
- `mode` (optional, default `hybrid`; values: `hybrid`, `text`, `vector`) — lets callers force a single ranking path when debugging
- `outcome` (optional, filter on `succeeded` or `failed`)
- `from`, `to` (optional, timestamp filters)

Hybrid ranking in v1 uses **Reciprocal Rank Fusion (RRF)** — the widely-adopted standard (Elasticsearch, Weaviate, Azure AI Search) for combining heterogeneous rankers without score normalization. The algorithm:

1. Pull top `N = candidate_multiplier × limit` candidates from BM25. The query text is converted with `websearch_to_tsquery('english', :q)` — never `to_tsquery(user_input)` — so arbitrary user/LLM strings cannot raise parse errors or smuggle operator syntax. Ranking uses `ts_rank_cd(content_tsv, websearch_to_tsquery(...))`.
2. Pull top `N` candidates from vector search. The query text is embedded, then ranked by cosine similarity over `content_vec`. The query includes `WHERE content_vec IS NOT NULL` so rows that landed with deferred embeddings (see [§ Embeddings](#embeddings)) are excluded from this branch — they remain findable via BM25.
3. For each document `d` in the union:
   ```
   rrf_score(d) = 1 / (k + bm25_rank(d)) + 1 / (k + vector_rank(d))
   ```
   Treat a missing rank as `+∞` (that side contributes `0`).
4. Sort by `rrf_score DESC`, then `created_at DESC` as tie-breaker.
5. Return the top `limit`.

**Platform-level constants in v1** (not tunable per agent):

- `k = 60` — the canonical RRF constant. Lower values bias more aggressively toward top-ranked hits; 60 is the default recommended by the original RRF paper and every mainstream hybrid-search system, and it works well for memory-scale corpora.
- `candidate_multiplier = 4` — pulls 4× the requested `limit` from each ranker before fusion. With a typical tool-side `limit=5`, the fusion pool tops out at 40 documents, keeping Postgres latency well inside the fast path while preserving recall.

**Pure-mode behavior:**

- `mode=text` → return top `limit` by BM25 rank alone.
- `mode=vector` → return top `limit` by cosine similarity alone.
- `mode=hybrid` (default) → RRF as above.

**Fallback:** If the `pgvector` extension is unavailable or the embedding provider is unreachable and `mode=hybrid` is requested, the endpoint silently degrades to BM25-only and signals this via `ranking_used: "text"` in the response. If `mode=vector` is explicitly requested under the same failure conditions, the endpoint returns a 503 (see below) — no silent fallback on an explicit request.

**Explicitly deferred to a later phase:**

- Temporal decay (OpenClaw uses a 30-day half-life; useful but adds a parameter to reason about — skip for v1).
- MMR / diversity re-ranking.
- Per-agent tuning of `k` or weights.
- Cross-encoder or LLM re-ranking.

Response shape:

```
{
  "results": [
    {
      "memory_id": "...",
      "title": "...",
      "summary_preview": "...",         // first ~200 chars
      "outcome": "succeeded",
      "task_id": "...",
      "created_at": "...",
      "score": 0.82
    }
  ],
  "ranking_used": "hybrid"              // "hybrid" | "text" | "vector"
}
```

The tool-surface equivalent of the 503 case returns a recoverable tool error (not a 503 — the graph stays in-loop), so the agent can fall back to `mode=text` itself when necessary.

`summary_preview` is a preview, not the full `summary`. Callers fetch the full entry via `GET /v1/agents/{agent_id}/memory/{memory_id}` when they want the body.

## Agent Tools

Two new built-in tools are registered for agents with `memory.enabled = true`. Plus one drill-down tool available regardless of `memory.enabled`.

**Tool-scope binding (applies to every tool below).** All three tools filter queries by `(tenant_id, agent_id)`, and this binding comes from the **worker's task context at tool-registration time** — never from LLM-supplied arguments. The SQL executed under the tool always includes `WHERE tenant_id = :bound AND agent_id = :bound` appended server-side by the tool implementation; the bound values come from the immutable task identity, not from anything the model can set. A confused or compromised agent cannot broaden its own scope by passing arguments. `task_history_get` is included in this rule even though it is available regardless of `memory.enabled` — a `task_id` guessed or leaked from logs cannot read another tenant's or another agent's task.

### `memory_note(text: string)`

Append an observation to the current task's draft memory entry.

- Arguments: `text` (required, 1–2048 chars)
- Returns: `{ "ok": true, "count": <current observation count> }`
- Cost: zero (no LLM, no network)
- Side effects: mutates LangGraph graph state; no DB write until final node

### `memory_search(query: string, limit: int = 5, mode: string = "hybrid")`

Search past memory entries for the current agent.

- Arguments: `query` (required), `limit` (optional, default 5, max 10 to keep result token footprint small), `mode` (optional)
- Returns: list of result objects with fields `memory_id`, `title`, `summary_preview`, `outcome`, `task_id`, `created_at`, `score`
- Scope: current task's `(tenant_id, agent_id)` only
- Cost: one embedding call if `mode` uses vector; BM25 is free
- The agent may call `memory_search` to find relevant entries, then call `task_history_get(task_id)` on a specific result to drill into the raw trace.

### `task_history_get(task_id: string)`

Fetch a bounded structured view of one past task. Available regardless of `memory.enabled` (a diagnostic tool, not a memory tool).

- Arguments: `task_id` (required)
- Scope: `(tenant_id, agent_id)` match required — no cross-agent access
- Returns:
  ```
  {
    "task_id": "...",
    "agent_id": "...",
    "input": "...",                      // truncated to ~2KB
    "status": "completed",
    "final_output": "...",               // truncated to ~2KB
    "tool_calls": [                      // compact list, each truncated
      { "name": "...", "args_preview": "...", "result_preview": "..." }
    ],
    "error_code": null,
    "error_message": null,
    "created_at": "...",
    "memory_id": "..."                   // linked memory entry if any
  }
  ```
- Large fields are truncated at well-known byte caps (~2 KB per field, list of tool calls capped at 20)
- Full raw message transcripts are **not** returned. If diagnostic depth beyond this is needed, the task's checkpoints are available via Console / existing status endpoints, not this tool.
- If the task does not exist or belongs to a different agent, returns a tool error (not a 404 HTTP response — the graph stays in-loop).

## API Surface

All endpoints are scoped by `(tenant_id, agent_id)` and require the same tenant auth used elsewhere in Phase 2.

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/v1/agents/{agent_id}/memory` | List memory entries (paginated). Filters: `outcome`, `from`, `to`. |
| `GET` | `/v1/agents/{agent_id}/memory/search` | Hybrid search (see [§ Search API](#search-api)). |
| `GET` | `/v1/agents/{agent_id}/memory/{memory_id}` | Full entry (title, summary, observations, tags, outcome, task_id, created_at). |
| `DELETE` | `/v1/agents/{agent_id}/memory/{memory_id}` | Delete one entry. Hard delete. |

Task submission (`POST /v1/tasks`) gains two optional fields:

```
attached_memory_ids: [uuid]    // optional; must belong to the submitting tenant + agent
skip_memory_write:   bool      // optional, default false; when true, no memory entry is written
                               //   for this specific task even if agent.memory.enabled = true
```

**`attached_memory_ids`:** Unknown ids, ids from another tenant, or ids from another agent all reject the submission with the same 4xx error shape (no distinction surfaced to caller). Resolved entries are injected into the task's initial prompt context at worker start.

**`skip_memory_write`:** When `true`, the worker treats the task exactly as if `agent.memory.enabled = false` for this task: no memory tools are registered (no `memory_note`, no `memory_search`), the `memory_write` node is absent from the graph, and the dead-letter memory hook is skipped. `task_history_get` remains available (it's a diagnostic tool, not memory-gated). Intended for per-call privacy opt-out — e.g., a task that will handle sensitive PII the customer does not want accumulating in the memory store — without needing to disable memory on the agent itself.

### List response shape (lightweight)

```
{
  "items": [
    {
      "memory_id": "...",
      "title": "...",
      "outcome": "succeeded",
      "task_id": "...",
      "created_at": "..."
    }
  ],
  "next_cursor": "...",
  "agent_storage_stats": {
    "entry_count": 4217,
    "approx_bytes": 12345678
  }
}
```

The `agent_storage_stats` block is included on the first page only and gives customers visibility into memory growth per agent.

### Detail response shape

Returns the full entry:

```
{
  "memory_id": "...",
  "agent_id": "...",
  "task_id": "...",
  "title": "...",
  "summary": "...",
  "observations": ["...", "..."],
  "outcome": "succeeded",
  "tags": ["..."],
  "summarizer_model_id": "...",
  "created_at": "...",
  "updated_at": "..."
}
```

## Console UX

### Memory tab on Agent detail page

Add a "Memory" tab to the existing agent detail route (`/agents/:agentId/memory`).

- List of memory entries, newest first
- Columns: title, outcome, task link, created_at, delete button
- Filters: outcome, date range, free-text query (uses `/memory/search`)
- Entry count + approximate storage at the top

Deletion uses a confirmation dialog. Failed and succeeded entries render with different outcome badges.

### Memory entry detail view

Opens on entry click. Shows:

- Title, outcome badge, created_at
- Summary (full)
- Observations (list, in order)
- Linked task — link to task detail
- Tags (if any)
- Delete action
- "Attach to new task" shortcut — navigates to submit page with this memory pre-attached

### Submit page extension

The submit page gains an optional "Attach past memories" affordance when the selected agent has `memory.enabled = true`:

- Search/select widget over the agent's memory entries (title + outcome + date)
- Multi-select
- Selected entries list inline before submit
- Token-footprint indicator (warn at e.g., 10 KB total, for awareness)

The indicator is informational — it does not block submission. Customers remain responsible for not over-attaching.

If the selected agent has `memory.enabled = false`, the attachment UI is hidden.

## Validation and Consistency Rules

- `agent_config.memory.enabled` is a boolean. If unset, treated as `false`.
- `agent_config.memory.summarizer_model`, when set, must reference an `active` row in the `models` table with the correct provider credentials resolvable. Validation runs at agent create/update time.
- `agent_config.memory.max_entries`, when set, must be an integer in `[100, 100_000]`. If unset, uses the platform default of `10_000`. Lower bound exists so customers cannot accidentally set it to a value that starves the Console UX; upper bound exists so HNSW rebuild stays within operational tolerances (see [§ Scale and Operational Plan](#scale-and-operational-plan)).
- `agent_config.memory` is a JSON sub-object on the existing `agents.agent_config` JSONB column. **API-layer implication:** the Java `AgentConfigRequest` record must be extended with an explicit `MemoryConfigRequest memory` field; Spring Boot's default Jackson configuration will otherwise fail on unknown properties (`FAIL_ON_UNKNOWN_PROPERTIES = true`), and the `AgentService.canonicalizeConfig` rebuilder will drop the field on round-trip. Track 5 updates `AgentConfigRequest`, `ConfigValidationHelper.validateAgentConfig`, and the canonicalization path to recognize the new sub-object, matching the pattern already used by `SandboxConfigRequest`.
- `attached_memory_ids` at submission must all resolve via a single SQL check `WHERE memory_id = ANY($1) AND tenant_id = :caller AND agent_id = :path_agent`. Any mismatch (unknown id, wrong tenant, wrong agent) rejects the submission with a uniform 4xx error that does **not** distinguish the cause. UUID non-enumerability is relied upon but is not a substitute for the scope predicate.
- `skip_memory_write` at submission is a per-task override; it never changes the agent's persisted configuration.
- `memory_note` inputs are bounded at 2 KB per call. Longer text is rejected by the tool with a usable error the agent can recover from. `memory_note` is only registered when the task-level effective setting is memory-enabled — i.e., `agent.memory.enabled = true AND NOT skip_memory_write`.
- `memory_search.limit` is capped at 10 when invoked as a tool, 20 via the REST API.
- `memory_search.q` is always converted via `websearch_to_tsquery('english', :q)` before use. Raw `to_tsquery` over user/LLM-controlled input is forbidden — it surfaces parse errors as a DoS/error-oracle.
- **404-not-403 disclosure rule applies globally.** Every path that could reveal existence of an entry or attachment across tenants or agents returns a uniform "not found" response: single-entry lookup (`GET /v1/agents/{agent_id}/memory/{memory_id}`), `DELETE`, list endpoints with filters that miss scope, search endpoints where the path `agent_id` does not exist, and tool errors from `memory_search` / `task_history_get`. Callers never learn whether an id exists, was deleted, or simply belongs to a scope they cannot see.
- **Memory-query invariant.** Every SQL query reading `agent_memory_entries` must include both `tenant_id = :bound` and `agent_id = :bound` predicates. The repository layer rejects queries missing either; a static-analysis / code-review rule enforces this at review time. Indexes are not partitioned by tenant, so these predicates are what pre-filters the vector/BM25 search — without them, HNSW may return cross-agent neighbors even at low `ef_search`.

## Embeddings

- The platform uses a single embedding provider for memory. Default: `text-embedding-3-small` (1536 dimensions).
- **Postgres version:** pgvector requires PG ≥ 12; HNSW requires pgvector ≥ 0.5.0 on PG ≥ 13. v1 pins pgvector ≥ 0.7 so that `halfvec` is available as a future-compatible option (not used in v1).
- The embedding provider key is stored alongside the existing LLM provider keys (Phase 2 retains the Phase 1 `provider_keys` model; Secrets-Manager-backed resolution is deferred to Phase 3+). No new credential mechanism is introduced by this track — the Model Discovery startup path is extended to validate the embedding key alongside chat-model keys.
- **Data-flow trust boundary.** Every memory write (normal, template-fallback, and dead-letter paths) ships the concatenated `title + summary + observations + tags` text to the embedding provider. This is the same trust boundary as the chat LLM provider and inherits the same provider-key hygiene from Phase 1. Template-fallback and dead-letter entries are subject to the same secret-scanning / redaction policy as chat LLM traffic — notably, a stray echoed API key in `final_output` will flow into both the stored `summary` and the embedding request.
- **Cost accounting.**
  - **Summarizer LLM at write time** — records a row in the existing `agent_cost_ledger` tied to the task's current checkpoint (`task_id` + `checkpoint_id` populated as with every other per-step LLM call). This fits the ledger's current schema unchanged.
  - **Embeddings at write time** — same story (task has a checkpoint to attribute against).
  - **Embeddings at search time via the REST `/v1/agents/{agent_id}/memory/search` endpoint** — these have **no** originating task or checkpoint, so they do **not** go into `agent_cost_ledger` in v1. The existing ledger schema (`0007_scheduler_and_budgets.sql`) requires `task_id` and `checkpoint_id` NOT NULL, and this track does not introduce schema changes there. API-driven search costs are recorded as **structured log lines** (`memory.search.embedding` with `tenant_id`, `agent_id`, token count, and cost in microdollars) — sufficient for detecting runaway usage in observability tools, deliberately not billable. A future track that cares about attributing API-driven costs can extend the ledger (e.g., nullable `task_id` plus a `source_kind` column) or introduce a separate `platform_cost_ledger`; Track 5 does not require that.
  - All rates are **zero-rated in v1** — either the ledger tracks non-billable usage (write-time) or structured logs do (search-time). No charge reaches customer budgets from memory internals.
- If the embedding provider is unavailable at write time, the entry is written with `content_vec = NULL` and a structured log line indicates the missing embedding. Search degrades to text-only for that entry; vector-branch queries include `WHERE content_vec IS NOT NULL` and simply skip these rows. A background reconciliation to backfill missing embeddings is **out of scope** for v1 — deferred embeddings remain `NULL` until explicitly reindexed.

## Scale and Operational Plan

Track 5's v1 shape (single shared table, global HNSW, unbounded entries up to the per-agent soft cap) is deliberately simple. It works well up to a moderate corpus (order of 100k–1M rows) and degrades gracefully past that. This section names where it stops scaling and the path forward.

**At what corpus size the v1 shape breaks:**

- **Global HNSW index build & memory footprint.** HNSW is graph-structured; pgvector's HNSW keeps the graph in `shared_buffers` for hot queries. At ~1M rows × 1536 dims × float32, the raw vector footprint alone is ~6 GB, and the HNSW graph adds on top of that. Concurrent index builds (e.g., from `REINDEX CONCURRENTLY`) can peak at 2–3× that. Past ~10M rows, single-node pgvector becomes operationally painful.
- **Global vs per-tenant recall.** Because indexes are not partitioned by `(tenant_id, agent_id)`, vector queries apply the tenant/agent filter as a post-HNSW predicate. At small corpora this is fine; at large corpora with heavily skewed tenant traffic, recall for smaller tenants can suffer because their entries may not reach the `ef_search` neighbor set.

**Scale-out plan (forward-compatible with v1):**

1. **Per-agent soft cap (10,000 entries, platform-max 100,000)** — already in Core Decisions. FIFO trim on insert keeps any single agent's corpus bounded. Most agents never cross the cap.
2. **Partial indexes for large tenants** (post-v1). If a single tenant produces most of the corpus, add partial HNSW indexes filtered by `tenant_id` for that tenant. pgvector supports partial indexes; switching to them is a non-breaking migration.
3. **Partitioning by `tenant_id` or `(tenant_id, agent_id)`** (later phase). Declarative `PARTITION BY HASH(tenant_id)` on `agent_memory_entries`, with per-partition HNSW indexes. This is the real scale-out answer but is a larger migration. The v1 schema does not preclude it.
4. **`halfvec` downgrade** (later phase). pgvector ≥ 0.7 supports `halfvec(1536)` at 2 bytes per dimension instead of 4; the index memory footprint halves with minimal recall loss. Can be flipped later with a column swap migration.

**Operational monitoring to add during implementation** (instrumentation enumerated now, dashboards deferred):

- Per-agent entry count and approximate bytes, surfaced via `agent_storage_stats` in the list response and logged at task completion.
- `memory_write` node end-to-end latency (summarizer + embedding + upsert commit).
- Embedding-provider error rate.
- `memory_search` p50 / p95 latency for each `mode`.
- HNSW `ef_search` effective and max-rows-scanned per query (to detect recall starvation).
- Count of rows with `content_vec IS NULL` per agent (deferred-embedding backlog).

## Cross-Track Coordination

- **Track 1 (Agent Control Plane):** `agent_config` JSON gains a `memory` section. No new columns on `agents`. `AgentConfig` validation schema updated.
- **Track 2 (Runtime State Model):** Terminal states (`completed`, `dead_letter`, `cancelled`) are already defined. Memory write reads final graph state and task terminal state; no new status values are introduced.
- **Track 3 (Scheduler & Budgets):** Summarizer LLM cost is recorded in `agent_cost_ledger` attributed to the task that produced the entry (see [§ Embeddings](#embeddings) and [§ Successful-task memory write — hybrid graph-node + worker commit](#successful-task-memory-write--hybrid-graph-node--worker-commit)). The summarizer call is **exempt from `budget_max_per_task` pause enforcement** to avoid a completed task ending up in `paused` state with a populated `pending_memory` but no DB row. `budget_max_per_hour` accounting still sees the cost, evaluated on the next task claim. The budget-enforcement path the worker runs between super-steps must skip the pause check for the super-step identified as `memory_write`. This carve-out is narrow, named by graph-node identity, and does not affect any other Track 3 enforcement rule.
- **Track 4 (BYOT):** Memory is platform-owned and unrelated to the custom tool runtime. `memory_note`, `memory_search`, and `task_history_get` are built-in tools, not BYOT tools.
- **Track 7 (Context Window Management, proposed):** Track 5 and Track 7 are independent for shipping purposes — neither blocks the other. There is, however, one deliberate design coupling worth naming:
  - **Pre-compaction memory flush.** When Track 7's Tier 3 summarization fires mid-task, the agent would otherwise lose anything in the about-to-be-compacted messages that it had not yet captured via `memory_note`. To mitigate this, Track 7 is expected to introduce a short "pre-compaction flush" hook: a full agentic turn (LLM call) immediately before Tier 3 runs, with a system instruction of the form *"Compaction is about to run. Use `memory_note` to save anything worth persisting that has not already been noted."* This is **not a synchronous platform-invoked tool call** — LangGraph tool invocation requires an `AIMessage.tool_calls`, so the flush literally is one extra LLM turn. It carries its own cost, latency, and context footprint, which Track 7's design is expected to budget and throttle.
  - **Implication for Track 5:** `memory_note` is designed to be callable from such system-triggered turns, not only from agent-initiated reasoning. The observations buffer is append-only and resilient to multiple calls throughout a task's life. The exact trigger design — when the flush fires, how it is throttled, whether it is opt-out — is deferred to Track 7's brainstorm.
  - If Track 7 ships first, the `memory_write` node runs on an already-compacted conversation, which is cheaper to summarize. If Track 7 ships after Track 5, memory write already works; Track 7 extends it with the flush hook without any Track 5 schema or tool change.
- **Agent Capabilities (sandbox/artifacts):** Orthogonal. Sandbox task inputs and artifact outputs are captured in the conversation like any other tool; the summarizer treats them the same.

## Development Environment Assumption

Track 5 introduces the `pgvector` Postgres extension, two new tables (`agent_memory_entries`, `task_attached_memories`), a generated column using `to_tsvector('english'::regconfig, …)`, and HNSW indexes. The runtime currently uses the stock `postgres:16` Docker image, which does **not** ship pgvector — so enabling this track requires coordinated changes to local dev, CI, and production Postgres environments.

**Image pin.** All Postgres environments switch from `postgres:16` to `pgvector/pgvector:pg16` (official pgvector distribution with matching PG major version). This applies to:

- `docker-compose.yml` for local dev (the `par-dev-postgres` service).
- The Makefile-driven isolated test database used by `make worker-test` and `make e2e-test`. The `test-db-up` target starts `par-e2e-postgres` via a standalone `docker run`, pulling the image from the `E2E_PG_IMAGE` variable — this must be retargeted to `pgvector/pgvector:pg16`. Updating `docker-compose.yml` alone is not sufficient; the Makefile path is a separate code path and is the one CI's `make worker-test` / `make e2e-test` actually exercises.
- `.github/workflows/ci.yml` service container definitions for any job that spins up Postgres directly instead of going through the Makefile.
- Whatever container/RDS instance backs staging and production. RDS offers pgvector as a native extension on PG ≥ 15; if the production target is a managed Postgres that does not include pgvector, this is a deploy blocker that must be resolved before Track 5 ships.

**Migration.** A new numbered migration file (next available slot, e.g. `0011_agent_memory.sql`) runs:

```sql
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE agent_memory_entries ( ... );
CREATE TABLE task_attached_memories ( ... );

-- Generated column using the two-arg to_tsvector form
-- (IMMUTABLE, required for STORED GENERATED)
ALTER TABLE agent_memory_entries
  ADD COLUMN content_tsv tsvector
  GENERATED ALWAYS AS (
    to_tsvector(
      'english'::regconfig,
      coalesce(title, '') || ' ' ||
      coalesce(summary, '') || ' ' ||
      array_to_string(observations, ' ') || ' ' ||
      array_to_string(tags, ' ')
    )
  ) STORED;

CREATE INDEX idx_memory_entries_tenant_agent_created
  ON agent_memory_entries (tenant_id, agent_id, created_at DESC);

CREATE INDEX idx_memory_entries_tsv
  ON agent_memory_entries USING GIN (content_tsv);

-- HNSW with pgvector default params (m=16, ef_construction=64)
CREATE INDEX idx_memory_entries_vec
  ON agent_memory_entries USING HNSW (content_vec vector_cosine_ops);
```

`CREATE EXTENSION vector` requires superuser privileges on most managed Postgres offerings; confirm the deploy role before shipping. Existing dev data does not need to be preserved — local dev databases will be re-initialized.

**Java API surface.** The `AgentConfigRequest` record and `ConfigValidationHelper.validateAgentConfig` are extended with an explicit `MemoryConfigRequest memory` nested object (see [§ Validation and Consistency Rules](#validation-and-consistency-rules)). Without this change, memory configuration posted via `POST /v1/agents` is rejected by Jackson before reaching the service layer.

## Acceptance Criteria

Track 5 is complete from a design perspective when all of the following are true:

1. A customer can enable memory for an agent and choose a summarizer model (or accept the platform default). The agent-level toggle is persisted at `agent_config.memory.enabled` with `max_entries` honoring the platform-default soft cap.
2. Every completed task with `memory.enabled = true` AND `skip_memory_write = false` writes exactly one entry containing a title, summary, and the agent's observations. Summarizer outage does not break this invariant — a template fallback entry is written instead, flagged via `summarizer_model_id = 'template:fallback'`.
3. `dead_letter` tasks with observations write a minimal template entry (`outcome = 'failed'`, `summarizer_model_id = 'template:dead_letter'`). `dead_letter` tasks with `dead_letter_reason = 'cancelled_by_user'`, and `dead_letter` tasks with no observations, write nothing.
4. Follow-up runs and redrives reusing the same `task_id` **overwrite** the prior memory entry. The entry always reflects the latest execution; `created_at` is preserved, `updated_at` and `version` advance. On follow-up, the graph state's `observations` is seeded from the existing row's `observations` column so earlier observations are not lost.
5. The agent can call `memory_note` during execution to append observations. Observations are durable at super-step checkpoint granularity and appear verbatim in the final entry alongside the retrospective summary.
6. The agent can call `memory_search` and receive ranked hybrid results (RRF, k=60, 4× candidate multiplier) scoped to its own `(tenant_id, agent_id)`. Tool scope is bound from the worker's task context, not from LLM arguments.
7. The agent can call `task_history_get(task_id)` and receive a bounded structured view of any past task in the same `(tenant_id, agent_id)`. Cross-agent or cross-tenant task ids return a tool-shaped "not found" error.
8. A customer can attach specific memory entries to a new task at submission. Attachment is validated via a single scoped SQL query; on any resolution miss the submission rejects with a uniform 4xx shape. Resolved attachments are persisted in `task_attached_memories` (with preserved `position`) and mirrored into the `task_submitted` event's `details` JSONB.
9. A customer can browse, search, read, and delete memory entries for each of their agents via the Console and the API. `agent_storage_stats` surfaces entry count and approximate bytes.
10. Cross-tenant and cross-agent access is rejected at the API layer with a uniform 404-shape response across list, single-entry lookup, search, delete, and tool-surface errors.
11. If `memory.enabled` is `false` OR task-level `skip_memory_write = true`, neither `memory_note` nor `memory_search` are registered, the `memory_write` node is absent from the graph, no entries are written, and no memory-related LLM/embedding cost is incurred. `task_history_get` remains available in all cases — it is a diagnostic drill-down tool, not a memory tool, and criterion 7 must hold regardless of memory state.
12. Memory write does not block task completion: on summarizer LLM failure, the template fallback writes the entry and the task still transitions to `completed`. On `pgvector` / embedding outage, the row is written with `content_vec = NULL` and search degrades to text-only.
13. When an agent reaches `max_entries`, FIFO trim removes the oldest entry in the same transaction as the new write; Console surfaces a warning at 80% of cap.
14. Summarizer LLM and embedding calls are recorded in `agent_cost_ledger`. Summarizer cost is exempt from `budget_max_per_task` pause enforcement (to avoid paused-with-pending-memory incoherence); embedding cost is zero-rated in v1.
15. All acceptance scenarios are covered by unit and E2E tests, including: memory disabled, memory enabled with successful write, summarizer-outage template fallback, embedding-outage deferred-vector path, dead-letter with and without observations, cancellation (`cancelled_by_user`), follow-up overwrite and observation seeding, redrive overwrite, cross-tenant attachment rejection, `memory_search` with all three modes, `task_history_get` scope enforcement, `max_entries` FIFO trim, and `skip_memory_write` at submission.
