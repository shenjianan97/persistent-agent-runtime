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
- Memory write runs as the **final LangGraph node** for `completed` tasks. For `dead_letter` tasks, a worker-side terminal hook writes a minimal entry **only if** the agent wrote at least one observation. For `cancelled` tasks, nothing is written.
- Memory is never auto-injected into a task's prompt. Retrieval is explicit only:
  - **Customer attaches** specific entries at task submission
  - **Agent calls** `memory_search` tool during execution
- **`memory_search` is the only cross-task retrieval tool.** Raw-trace access is drill-down-only via `task_history_get(task_id)`, which returns a bounded structured view of one task.
- Summarization LLM cost counts against the agent's Track 3 budget, using a configurable `summarizer_model` (falls back to a platform default).
- Access is scoped to the customer that owns the agent. Cross-tenant access is forbidden at the API layer.
- Retention in v1 is **unbounded**. Deletion is explicit only — customers delete individual entries via API. Auto-retention policies are deferred.

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
| `content_tsv` | `tsvector` | Generated column over `title \|\| summary \|\| array_to_string(observations, ' ') \|\| array_to_string(tags, ' ')`, GIN-indexed |
| `content_vec` | `vector(1536)` | Embedding of the same concatenated text, HNSW-indexed. Dimension matches platform default embedding model. |
| `summarizer_model_id` | `TEXT` | Nullable, records which model generated the summary. Sentinel values: `'template:fallback'` when the LLM was unavailable and a template summary was used, `'template:dead_letter'` for the dead-letter path. |
| `created_at` | `TIMESTAMPTZ` | NOT NULL, defaults to `now()`, immutable once set |
| `updated_at` | `TIMESTAMPTZ` | NOT NULL, defaults to `now()`, rewritten on each upsert (follow-up / redrive / regeneration) |

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

### Tasks table extension

Track 5 adds one column to the existing `tasks` table so the attachment set is a first-class, queryable task field rather than something reconstructed from event payloads.

| Column | Type | Constraints / Meaning |
|--------|------|------------------------|
| `attached_memory_ids` | `UUID[]` | NOT NULL, default `'{}'`, set at task submission from the `attached_memory_ids` request field. Each id must belong to the submitting `(tenant_id, agent_id)` at submission time — validated by the API. Immutable after task creation (not updated on follow-up). |

This column is not a FK to `agent_memory_entries.memory_id` — customer delete of a memory entry leaves a dangling id in the task row, which is acceptable because the entry was already injected into the task's initial context at submission time and the audit trail is the source of truth, not the live entry.

The `task_submitted` event's `details` JSONB also includes the id list so a pure event-timeline consumer can reconstruct attachment history without joining the `tasks` row. Storing in both places is a deliberate small duplication: the `tasks` column is cheap to query; the event record is immutable.

Task detail API responses include `attached_memory_ids` and, for human-friendly rendering, a derived `attached_memories_preview: [{memory_id, title}]` resolved from the current `agent_memory_entries` rows (ids that no longer resolve are omitted from the preview but remain in the raw array).

### pgvector extension

The `pgvector` extension must be enabled on the Postgres database. Embedding dimension is fixed at **1536** in v1, matching the default embedding model (`text-embedding-3-small` or equivalent; see [§ Embeddings](#embeddings)).

## Write Path

### Mid-task writes (optional)

The agent can call `memory_note(text: string)` during task execution. Each call appends one string to an in-memory `observations` list held in the LangGraph graph state. The tool:

- returns immediately (no LLM call, no I/O beyond graph-state mutation)
- rejects empty strings and strings longer than a per-note cap (e.g., 2 KB)
- is registered only when `memory.enabled` is `true` for the task's agent

Observations live in graph state throughout execution. They are not persisted as their own rows. Observations that were present at the time of the most recent LangGraph super-step checkpoint survive worker crashes and are available when the task next runs — including during the dead-letter hook below. Observations appended mid-step and not yet checkpointed are lost on crash, but the raw trace for recently-attempted steps remains available via `task_history_get`.

### Final memory-write node (completed tasks)

A dedicated terminal LangGraph node runs as the last step before graph termination for every task where `memory.enabled` is `true`:

1. Read the full conversation state and the accumulated `observations` list.
2. Call the `summarizer_model` with a prompt that:
   - Instructs it to produce a `title` (≤10 words, action-oriented) and a `summary` (≤400 words, retrospective)
   - Passes the observations so the summary complements, not duplicates, them
3. **Upsert** a row into `agent_memory_entries` (see [§ Concurrency and idempotency](#concurrency-and-idempotency)) with `outcome = 'succeeded'`, observations preserved verbatim, and summary/title from the LLM.
4. Compute and store the embedding (`content_vec`) in the same transaction. `content_tsv` is a generated column so no separate write is needed.

LLM cost for this call is recorded against the task via the existing Track 3 cost-ledger mechanism. The cost is attributed to the task that produced the entry.

**Summarizer-unavailable fallback.** If the `summarizer_model` call fails after its internal retries — provider outage, rate limit, credential rotation lag — the node does **not** skip the write. Instead it falls back to a template-generated entry and still upserts a row:

- `title`: `"Completed: <first 80 chars of task input, sanitized>"`
- `summary`: `"<final_output truncated to ~1KB> [summary generation unavailable; review observations and linked task trace for detail.]"`
- `observations`: preserved verbatim
- `outcome`: `'succeeded'` (the task did succeed — only the summary is degraded)
- `summarizer_model_id`: `'template:fallback'`

The failure is recorded in a structured log line so operators can detect sustained summarizer outages. The embedding step still runs on the template text; if the embedding provider is also down, the row is written with `content_vec = NULL` as described in [§ Embeddings](#embeddings). The task itself still transitions to `completed` regardless. The invariant every completed memory-enabled task produces exactly one memory entry is preserved across both normal and degraded paths.

Customers can later regenerate a proper summary for `'template:fallback'` entries once the provider recovers. A regeneration endpoint is **out of scope for v1** — the template entry remains until manually deleted or overwritten by a follow-up run.

### Dead-letter hook (failed tasks with observations)

As part of the worker's dead-letter finalization path — after max retries are exhausted and before the task's status is updated to `dead_letter` — a hook runs:

1. Read the task's last LangGraph checkpoint to recover the `observations` list.
2. If `observations` is empty → skip, no memory entry is written.
3. If `observations` is non-empty:
   - Auto-generate a minimal summary: `"Task dead-lettered after <N> retries: <last_error_code> — <last_error_message>"` (no LLM call; template-generated from task fields).
   - Generate a short title template: `"[Failed] <first_50_chars_of_task_input>"`.
   - Upsert a row with `outcome = 'failed'`, observations preserved, summary and title from the template, `summarizer_model_id = 'template:dead_letter'`.
   - Compute embedding over the same concatenated text.

The rationale: if the agent flagged something mid-execution, the task got far enough to learn something worth remembering. If nothing was flagged, the failure is likely trivial and memory noise outweighs signal.

No LLM is called on the dead-letter path. Cost is effectively zero beyond the embedding call.

### Cancelled tasks

`cancelled` tasks never write a memory entry. Observations in graph state are discarded. Rationale: the customer explicitly aborted the task — any residue in future contexts would be unwelcome.

### Concurrency and idempotency

- `UNIQUE (task_id)` on `agent_memory_entries` ensures at most one entry per task at any time.
- Memory writes use **upsert**, not insert-or-skip:
  ```
  INSERT INTO agent_memory_entries (...) VALUES (...)
  ON CONFLICT (task_id) DO UPDATE SET
    title = EXCLUDED.title,
    summary = EXCLUDED.summary,
    observations = EXCLUDED.observations,
    outcome = EXCLUDED.outcome,
    tags = EXCLUDED.tags,
    content_vec = EXCLUDED.content_vec,
    summarizer_model_id = EXCLUDED.summarizer_model_id,
    updated_at = now();
  ```
- **Why upsert, not insert-once:** Track 4's follow-up endpoint (`POST /v1/tasks/{task_id}/follow-up`) resumes a completed task under the same `task_id`. A dead-letter-then-redrive sequence also reuses the same `task_id`. In both cases the memory entry must reflect the latest execution, not be permanently frozen to the first attempt's outcome. An insert-once model would leave stale or failed entries in place forever after follow-up or successful redrive.
- **Duplicate-write within a single execution** (e.g., the final node is re-entered after a transient crash before it committed): upsert is still safe. The second call overwrites the first with the same data; `created_at` is preserved, `updated_at` advances.
- **Ordering guarantee:** the final memory-write node (or the dead-letter hook) runs once per execution attempt, so there is no intra-attempt race. Cross-execution upserts are inherently serialized by the task's own lifecycle — only one worker holds a lease at a time.
- **`created_at` vs `updated_at`:** `created_at` is the first time a memory row was written for this `task_id`. `updated_at` is the most recent execution's write time. The Console renders both.

## Read Path

### Retrieval is always explicit

Memory entries are never auto-loaded into a task's prompt. There are exactly three ways an entry is surfaced:

1. **Customer attach at submission** — `POST /v1/tasks` accepts `attached_memory_ids: [uuid]`. The API resolves each id against the submitting tenant+agent, fails fast on unknown/wrong-agent ids, and copies the resolved `title + observations + summary` blocks into the task's initial prompt context (system-prompt-prefix). The ids are persisted on the task row (see [§ Tasks table extension](#tasks-table-extension)) so the Console, API, and audit consumers can always reconstruct which memories were injected into a given run.
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

1. Pull top `N = candidate_multiplier × limit` candidates from BM25 (`ts_rank_cd` over `content_tsv`).
2. Pull top `N` candidates from vector search (cosine similarity over `content_vec`).
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

Task submission (`POST /v1/tasks`) gains one optional field:

```
attached_memory_ids: [uuid]    // optional; must belong to the submitting tenant + agent
```

Unknown ids, ids from another tenant, or ids from another agent reject the submission with a 4xx error. Resolved entries are injected into the task's initial prompt context at worker start.

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
- `attached_memory_ids` at submission must all resolve to entries within the submitting `(tenant_id, agent_id)`. Any mismatch rejects the submission.
- `memory_note` inputs are bounded at 2 KB per call. Longer text is rejected by the tool with a usable error the agent can recover from.
- `memory_search.limit` is capped at 10 when invoked as a tool, 20 via the REST API.
- Entry lookups by `memory_id` include an implicit `tenant_id = :caller_tenant AND agent_id = :path_agent` filter. A mismatched id returns 404, not 403, to avoid leaking existence.

## Embeddings

- The platform uses a single embedding provider for memory. Default: `text-embedding-3-small` (1536 dimensions).
- The embedding provider key is stored alongside the existing LLM provider keys (Phase 2 retains the Phase 1 `provider_keys` model; Secrets-Manager-backed resolution is deferred to Phase 3+). No new credential mechanism is introduced by this track — the Model Discovery startup path is extended to validate the embedding key alongside chat-model keys.
- Embedding calls happen at memory-write time (one per entry) and at `memory_search` time when `mode` includes vector (one per search). Both costs are platform-internal in v1 and not charged back to customers — they are small relative to the summarizer LLM call.
- If the embedding provider is unavailable at write time, the entry is written with `content_vec = NULL` and a structured log line indicates the missing embedding. Search degrades to text-only for that entry. A background reconciliation to backfill missing embeddings is **out of scope** for v1 — deferred embeddings remain `NULL` until explicitly reindexed.

## Cross-Track Coordination

- **Track 1 (Agent Control Plane):** `agent_config` JSON gains a `memory` section. No new columns on `agents`. `AgentConfig` validation schema updated.
- **Track 2 (Runtime State Model):** Terminal states (`completed`, `dead_letter`, `cancelled`) are already defined. Memory write reads final graph state and task terminal state; no new status values are introduced.
- **Track 3 (Scheduler & Budgets):** Summarizer LLM cost is recorded via the existing `agent_cost_ledger` mechanism, attributed to the task that produced the entry. Hourly and per-task budget enforcement applies to the final memory-write call like any other step.
- **Track 4 (BYOT):** Memory is platform-owned and unrelated to the custom tool runtime. `memory_note`, `memory_search`, and `task_history_get` are built-in tools, not BYOT tools.
- **Track 7 (Context Window Management, proposed):** Track 5 and Track 7 are independent for shipping purposes — neither blocks the other. There is, however, one deliberate design coupling worth naming:
  - **Pre-compaction memory flush.** When Track 7's Tier 3 summarization fires mid-task, the agent would otherwise lose anything in the about-to-be-compacted messages that it had not yet captured via `memory_note`. To mitigate this, Track 7 is expected to introduce a short "pre-compaction flush" hook: a separate agentic turn immediately before Tier 3 runs, with a system instruction of the form *"Compaction is about to run. Use `memory_note` to save anything worth persisting that has not already been noted."* This reuses Track 5's existing `memory_note` tool and its graph-state-backed observations buffer — no new Track 5 primitive is required.
  - **Implication for Track 5:** `memory_note` is designed to be callable from such system-triggered turns, not only from agent-initiated reasoning. The observations buffer is append-only and resilient to multiple calls throughout a task's life. The exact trigger design — when the flush fires, how it is throttled, whether it is opt-out — is deferred to Track 7's brainstorm.
  - If Track 7 ships first, the final memory-write node runs on an already-compacted conversation, which is cheaper to summarize. If Track 7 ships after Track 5, memory write already works; Track 7 extends it with the flush hook.
- **Agent Capabilities (sandbox/artifacts):** Orthogonal. Sandbox task inputs and artifact outputs are captured in the conversation like any other tool; the summarizer treats them the same.

## Development Environment Assumption

Track 5 introduces a new Postgres extension (`pgvector`) and a new table. Schema changes may be folded into the existing SQL files. Existing dev data does not need to be preserved; local dev databases will be re-initialized. The CI workflow must enable `pgvector` on the E2E Postgres service container.

## Acceptance Criteria

Track 5 is complete from a design perspective when all of the following are true:

1. A customer can enable memory for an agent and choose a summarizer model (or accept the platform default).
2. Every completed task with memory enabled writes exactly one entry containing a title, summary, and the agent's observations. Summarizer outage does not break this invariant — a template fallback entry is written instead, flagged via `summarizer_model_id = 'template:fallback'`.
3. `dead_letter` tasks with observations write a minimal entry (`outcome = 'failed'`, `summarizer_model_id = 'template:dead_letter'`); `dead_letter` tasks without observations, and all `cancelled` tasks, write nothing.
4. Follow-up runs and redrives reusing the same `task_id` **overwrite** the prior memory entry. The entry always reflects the latest execution; `created_at` is preserved, `updated_at` advances.
5. The agent can call `memory_note` during execution to append observations that appear verbatim in the final entry alongside the retrospective summary.
6. The agent can call `memory_search` and receive ranked hybrid results (RRF, k=60, 4× candidate multiplier) scoped to its own agent.
7. The agent can call `task_history_get(task_id)` and receive a bounded structured view of any past task on the same agent.
8. A customer can attach specific memory entries to a new task at submission; the resolved entries are injected into the task's initial context. The submitted id set is persisted on the task row (`attached_memory_ids`) and in the `task_submitted` event details, so the audit trail of which memories were injected into a given run is queryable after the fact.
9. A customer can browse, search, read, and delete memory entries for each of their agents via the Console and the API.
10. Cross-tenant and cross-agent access is rejected at the API layer and invisible to the caller (404, not 403).
11. If `memory.enabled` is `false`, no memory tools are registered, no final node runs, no entries are written, and no cost is incurred.
12. The memory write does not block task completion — on summarizer failure, the template fallback writes the entry and the task still transitions to `completed`.
13. All acceptance scenarios are covered by unit and E2E tests, including: memory disabled, memory enabled / successful write, summarizer-outage template fallback, dead-letter with and without observations, cancellation, follow-up overwrite, redrive overwrite, concurrent task completion, cross-tenant attachment rejection, and search degradation when pgvector is unavailable.
