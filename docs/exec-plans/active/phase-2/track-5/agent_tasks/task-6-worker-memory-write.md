<!-- AGENT_TASK_START: task-6-worker-memory-write.md -->

# Task 6 — Worker Memory Write Path (Graph Node + Worker Commit)

## Agent Instructions

You are a software engineer implementing one module of a larger system. Your scope is strictly limited to this task.

**CRITICAL PRE-WORK:** Before beginning implementation, you MUST read:
1. `docs/design-docs/phase-2/track-5-memory.md` — sections "Graph state extension", "Successful-task memory write — hybrid graph-node + worker commit", "Concurrency and idempotency", "Validation and Consistency Rules", and "Cross-Track Coordination" (Track 3 carve-out).
2. `services/worker-service/executor/graph.py` — entire file. Particularly: `_build_graph`, `execute_task`, the `tools_condition` edge from `agent → END`, the post-astream commit path that ends a task, the existing `_handle_dead_letter`, and the Track 3 per-step budget enforcement.
3. `services/worker-service/executor/embeddings.py` (from Task 5) — the `compute_embedding` helper and `EmbeddingResult` type.
4. `services/worker-service/core/worker.py` — the lease-validated `UPDATE tasks SET status='completed'` path that already concludes successful tasks.
5. `services/worker-service/checkpointer/` — how super-step checkpoints work and how `aget_tuple` retrieves the final state.
6. Task 2 agent-config extension — the shape of `agent_config.memory` the worker reads.
7. `services/worker-service/core/db.py` — asyncpg pool usage and transaction patterns.

**CRITICAL POST-WORK:** After completing this task:
1. Run `make worker-test` and `make e2e-test`. Fix any regressions. Specifically confirm that memory-disabled agents are behaviourally unchanged (no new state field, no new graph node, no new DB reads, no new cost).
2. Update the status in `docs/exec-plans/active/phase-2/track-5/progress.md` to "Done".

## Context

This is the core of Track 5. For a task whose agent has `memory.enabled=true` AND the task's `skip_memory_write` override is `false`:

- The graph gains a `memory_write` node on the "no pending tool calls" branch (so `agent → tools | END` becomes `agent → tools | memory_write → END`).
- `memory_write` produces a title + summary + embedding and places them on a `pending_memory` state field. LangGraph's checkpointer makes this crash-recoverable.
- The worker's existing post-astream commit path reads `pending_memory` from the final state, then UPSERTs the memory row and updates the task to `completed` in **one lease-validated transaction**. FIFO trim happens in the same transaction when the INSERT branch would push the agent past `max_entries`.
- Summarizer outage triggers a template-fallback entry (still `outcome='succeeded'`); embedding outage results in `content_vec=NULL`. The invariant "every completed memory-enabled task produces exactly one entry" must hold across all paths.
- The Track 3 per-step budget enforcement the worker runs between super-steps skips the per-task pause check specifically for the `memory_write` super-step (named carve-out). Hourly spend still accrues.

Dead-letter, follow-up seeding, and attached-memory injection are Task 8. Memory tools (`memory_note`, etc.) are Task 7. This task delivers only the graph node, the state schema, the worker commit path, and the budget carve-out.

## Task-Specific Shared Contract

- **Effective-memory gate:** `effective_memory_enabled = agent_config.memory.enabled AND NOT task.skip_memory_write`. When `false`, the worker behaves as today (no new state, no new node, no commit-path branch). This gate is computed once per task in `execute_task` and is the single predicate every subsequent memory branch checks.
- **State schema when memory is enabled:**
  ```
  class MemoryEnabledState(MessagesState):
      observations: Annotated[list[str], operator.add]
      pending_memory: dict | None
  ```
  Register this as a custom state schema on the graph. When memory is disabled, keep using plain `MessagesState` (no schema change). Task 7 will have `memory_note` return `Command(update={"observations": [note]})` — the `operator.add` reducer here is what lets that be associative.
- **Graph topology when memory is enabled:**
  ```
  agent ──[pending tool calls?]──┬──► tools ──► agent
                                 │
                                 └──► memory_write ──► END
  ```
  Use `tools_condition` (or its equivalent) to route, but replace the "no pending tool calls" target with `memory_write`. Memory-disabled agents keep the original `agent → END` edge.
- **`memory_write` node responsibilities:**
  1. Read `messages` and `observations` from state.
  2. Call the summarizer LLM — `agent_config.memory.summarizer_model` if set, else the platform default (e.g., a cheap Haiku-class model configured by env var). Pass the observations to the prompt so the summary complements rather than duplicates them.
  3. Build the concatenated text `title + summary + observations + tags` and call `compute_embedding` (Task 5).
  4. Return `Command(update={"pending_memory": {...}})` with keys: `title` (≤10 words), `summary` (≤400 words), `outcome="succeeded"`, `content_vec` (or `None`), `summarizer_model_id` (model id or `"template:fallback"`), `observations_snapshot` (list verbatim), `tags` (list, may be empty — v1 does not auto-generate tags).
  5. **No DB writes from the node itself** — only state mutation. LangGraph persists via the normal super-step checkpoint.
- **Summarizer outage handling:** retry per the summarizer-model LangChain client's default. On exhaustion, build `pending_memory` from a template — `title = "Completed: <first 80 chars of task input>"`, `summary = "<final_output truncated to ~1KB> [summary generation unavailable; review observations and linked task trace for detail.]"`, `summarizer_model_id = "template:fallback"`, `outcome = "succeeded"`. The node returns the same shape — no separate "fallback" branch in the caller.
- **Embedding outage handling:** if `compute_embedding` returns `None`, populate `pending_memory.content_vec = None`. The caller writes the row with `content_vec = NULL`. Log `memory.write.embedding_deferred` once.
- **Commit transaction (in `worker.py` or the post-astream helper):**
  ```
  BEGIN;
    -- 1) Read pending_memory from final state (via aget_tuple on thread_id).
    -- 2) UPSERT with RETURNING: capture both the new row's memory_id and
    --    whether this was an INSERT (xmax = 0) or an UPDATE (xmax != 0).
    --    Pattern:
    --      INSERT INTO agent_memory_entries (…) VALUES (…)
    --      ON CONFLICT (task_id) DO UPDATE SET
    --        title=EXCLUDED.title, summary=EXCLUDED.summary, … ,
    --        updated_at=now(), version = agent_memory_entries.version + 1
    --      RETURNING memory_id, (xmax = 0) AS inserted;
    -- 3) If inserted = TRUE AND current_count > max_entries:
    --      DELETE FROM agent_memory_entries
    --      WHERE memory_id IN (
    --        SELECT memory_id FROM agent_memory_entries
    --        WHERE tenant_id=$1 AND agent_id=$2 AND memory_id != :just_inserted
    --        ORDER BY created_at ASC, memory_id ASC
    --        LIMIT (current_count - max_entries)
    --      );
    --    The `memory_id != :just_inserted` predicate guarantees the row just
    --    written by step 2 cannot itself be evicted. The ORDER BY tiebreak
    --    on memory_id makes the eviction deterministic when multiple rows
    --    share a created_at timestamp.
    --    current_count is read AFTER the UPSERT, inside the same transaction,
    --    via a scoped SELECT COUNT(*). Per-task serialisation is guaranteed
    --    because only one worker holds the task lease at a time — concurrent
    --    writes from OTHER tasks are possible but land in independent
    --    transactions and race-resolve via MVCC; at worst the trim evicts
    --    one too few rows in a given transaction, and the next task's
    --    commit catches up.
    -- 4) UPDATE tasks SET status='completed', … WHERE task_id=$t AND lease_owner=:me;
    -- 5) COMMIT.
  ```
  - All four (UPSERT, optional TRIM, task UPDATE, COMMIT) are in one transaction. Lease validation on step 4 rolls back the whole thing if the worker lost the lease.
  - `created_at` must NOT be in the UPSERT's `DO UPDATE SET` clause. `updated_at = now()`; `version = agent_memory_entries.version + 1`.
  - Trim fires ONLY when `inserted = TRUE`. The ON CONFLICT → UPDATE branch leaves the row count unchanged and MUST NOT trigger trim.
  - Emit `memory.write.trim_evicted` with the evicted count when trim fires.
- **Budget carve-out (Track 3 interaction):** The Track 3 per-step budget enforcement the worker runs after each super-step must **skip** the per-task pause check if and only if the super-step that just completed is the `memory_write` node. Identify the node by name (do not use heuristics on cost magnitude). `budget_max_per_hour` accounting still receives the cost at the normal point.
  - **Where the carve-out lives:** Track 3's per-step enforcement was implemented in the worker post-super-step hook in `services/worker-service/executor/graph.py` (look for the per-step cost check that raises / triggers the pause transition). Read that path end-to-end before adding the carve-out; do NOT invent a new enforcement file. If the Track 3 path has since moved (check git history), follow the rename.
- **Cost ledger:** write one row per LLM call (summarizer) and one row per embedding call (`compute_embedding` succeeded). Attribute to the task's current checkpoint id. Both go into the existing `agent_cost_ledger` — schema unchanged. Cost values come from the `models` table (summarizer) and the helper / pricing constant (embedding — see Task 5). A failed `compute_embedding` call (returned `None`) produces no ledger row.
- **`tags` field:** reserved in the schema for forward-compatibility but **unused in v1**. Always set to `[]` in `pending_memory`. No agent-facing tag tool or API. Document this as a single line in `memory_graph.py`.
- **Platform-default summarizer literal:** `PLATFORM_DEFAULT_SUMMARIZER_MODEL` reads from env var `MEMORY_DEFAULT_SUMMARIZER_MODEL`. The compiled-in fallback is `"claude-haiku-4-5"` — cheap Haiku-class per design doc. Document the env-var default in `services/worker-service/README.md` as part of this task.
- **`operator.add` reducer heads-up:** Track 5 is the first feature in this worker to register a custom state schema with a LangGraph reducer. There is no in-repo precedent — the stock `MessagesState` is used elsewhere. Cite the LangGraph documentation for `Annotated[list[str], operator.add]` in a brief comment so future readers understand the pattern was imported, not copied from a neighbour.
- **Crash recovery matrix:**
  - Mid-summarizer LLM call: checkpoint does not advance; reaper re-claim resumes the graph; `memory_write` retries. The summarizer LLM provider is idempotent for this use — a repeated call with the same inputs is fine.
  - After `memory_write` state update but before worker commit: checkpoint has `pending_memory`; reaper re-claim; worker reads final state and reruns the commit tx. UPSERT absorbs any race.
  - Lease lost mid-commit: UPDATE fails predicate; rollback; reaper re-claim; retries.
- **HITL pauses and budget pauses:** the `memory_write` node is on the terminal branch only. It does NOT fire when the agent is paused (those exit the graph via different paths). No code change is required here beyond "only add the edge on the `tools_condition` no-pending-calls branch" — verify with a test that a HITL-paused task does not traverse `memory_write`.

## Affected Component

- **Service/Module:** Worker Service — Executor + Post-Astream Commit
- **File paths:**
  - `services/worker-service/executor/memory_graph.py` (new — `MemoryEnabledState`, `memory_write_node`, `build_pending_memory_template_fallback`)
  - `services/worker-service/executor/graph.py` (modify — graph assembly, topology change, budget carve-out identification, gating on `effective_memory_enabled`)
  - `services/worker-service/core/worker.py` (modify — post-astream commit path: UPSERT + trim + lease-validated task UPDATE)
  - `services/worker-service/core/memory_repository.py` (new — asyncpg helpers: `upsert_memory_entry`, `trim_oldest_if_over_cap`, `count_entries_for_agent`, `read_pending_memory_from_final_state`)
  - `services/worker-service/tests/test_memory_write.py` (new)
- **Change type:** new code + modification

## Dependencies

- **Must complete first:** Task 2 (agent-config shape), Task 5 (embedding helper), Task 1 (schema).
- **Provides output to:** Task 7 (tool registration uses `effective_memory_enabled` and the custom state schema), Task 8 (dead-letter hook branches from the same commit path; follow-up seeding reads existing memory rows).
- **Shared interfaces/contracts:** `MemoryEnabledState`; `memory_repository.*` helpers; the node name `memory_write` (used by Task 3 budget carve-out identification).
- **Parallel-safety:** Tasks 7 and 8 both edit `services/worker-service/executor/graph.py`. If dispatched concurrently, use `isolation: "worktree"` on one or more agents and merge on completion per AGENTS.md §Parallel Subagent Safety.

## Implementation Specification

### `memory_graph.py` contents (contract)

- `MemoryEnabledState` class as specified above.
- `memory_write_node(state, config)` async function: reads state, calls summarizer, calls `compute_embedding`, returns a `Command` updating `pending_memory`. Honours the summarizer-outage fallback shape.
- `build_pending_memory_template_fallback(messages, observations, task_input, final_output)` helper — used by the node when the summarizer retries exhaust.
- `PLATFORM_DEFAULT_SUMMARIZER_MODEL` constant (read from env var `MEMORY_DEFAULT_SUMMARIZER_MODEL`, fallback to a documented literal — coordinate with ops).

### `graph.py` modifications (contract)

- Compute `effective_memory_enabled` once per task.
- When `true`:
  - Use `MemoryEnabledState` as the graph's state type.
  - Add the `memory_write` node and the `agent → memory_write → END` edge on the "no pending tool calls" branch; keep the `agent → tools → agent` cycle.
- When `false`: no change to graph assembly.
- In the Track 3 per-super-step budget enforcement loop, annotate the last completed super-step's node name; skip the per-task pause check if that name is `memory_write`. Keep the hourly-spend ledger write.

### `worker.py` commit modifications (contract)

- After `astream` completes and the task is on the successful terminal branch:
  - If `effective_memory_enabled`:
    - Read `pending_memory` from the final state (via `aget_tuple(thread_id)`).
    - Open the single transaction described above.
    - If `pending_memory` is `None` (should not happen on the successful path, but guard it), log `memory.write.missing_pending` and skip the memory UPSERT — still commit the task UPDATE. This is a safety net, not expected.
    - Otherwise UPSERT memory row, optionally trim, UPDATE task, COMMIT.
  - Else: the existing single-statement `UPDATE tasks SET status='completed' WHERE …` path runs unchanged.

### `memory_repository.py` contents (contract)

- Pure asyncpg helpers. No LangGraph awareness. No transaction management — callers own the connection and transaction.
- `upsert_memory_entry(conn, entry: dict) -> {"inserted": bool, "memory_id": UUID}` — uses the UPSERT SQL from the design doc "Concurrency and idempotency" section. Returns whether the row was inserted (vs updated) via `xmax = 0` test.
- `trim_oldest(conn, tenant_id, agent_id, over_count) -> int` — deletes oldest rows, returns count evicted.
- `max_entries_for_agent(agent_config) -> int` — returns `agent_config.memory.max_entries` clamped to `[100, 100_000]` or the platform default `10_000`.
- `read_pending_memory_from_checkpoint(checkpointer, thread_id) -> dict | None`.

### Logging

- `memory.write.started` when the node begins (tenant, agent, task).
- `memory.write.summarizer_failed` on final fallback.
- `memory.write.embedding_deferred` when `content_vec = None`.
- `memory.write.committed` with `tokens`, `latency_ms`, `inserted` (bool), `trim_evicted` (int), `content_vec_null` (bool).
- `memory.write.trim_evicted` with the evicted count and the trigger agent.

## Acceptance Criteria

- [ ] Memory-disabled agents (no `memory` config OR `memory.enabled=false`) behave identically to pre-Track-5 — confirmed by an unchanged-golden-path test on an existing task fixture.
- [ ] Memory-enabled agents running a task that goes to the terminal branch traverse the `memory_write` node, and the final state contains `pending_memory` with a title, summary, embedding (or `None`), and observations snapshot.
- [ ] After successful execution, the task has `status='completed'` AND exactly one row exists in `agent_memory_entries` for that `task_id`.
- [ ] Summarizer outage (mocked) results in a row with `summarizer_model_id='template:fallback'` and the task still completes.
- [ ] Embedding outage (mocked) results in a row with `content_vec IS NULL` and the task still completes.
- [ ] Follow-up / redrive (same `task_id`) UPSERTs — `created_at` preserved, `updated_at` advances, `version` increments.
- [ ] FIFO trim fires when an agent reaches `max_entries` — the oldest row is deleted in the same transaction as the new INSERT. UPDATE-branch writes do NOT trigger trim.
- [ ] Trim evicted count is reported in `memory.write.trim_evicted`.
- [ ] Per-task budget enforcement does not pause a task on the `memory_write` super-step cost — confirmed by a test that sets `budget_max_per_task` below the summarizer cost but above the regular-step cost.
- [ ] `budget_max_per_hour` accounting still records the memory-write cost.
- [ ] HITL pauses and budget pauses (on regular steps) do NOT traverse `memory_write` — verified by a pause-and-resume test.
- [ ] Crash between `memory_write` state update and worker commit is recoverable: after reaper re-claim, the UPSERT absorbs and the task completes with a single row.
- [ ] Crash mid-commit (lease revoked) rolls back; after reaper re-claim, retry succeeds.
- [ ] Each successful summarizer LLM call writes an `agent_cost_ledger` row (task_id + current checkpoint id, cost in microdollars).
- [ ] Each successful embedding call writes an `agent_cost_ledger` row on the same attribution keys. A failed `compute_embedding` (returned `None`) writes NO ledger row.
- [ ] `pending_memory.tags` is always `[]` in v1 — confirmed by assertion on the committed row.
- [ ] `make worker-test` and `make e2e-test` pass.

## Testing Requirements

- **Unit tests** (`test_memory_write.py`):
  - `effective_memory_enabled` truth-table (agent on/off × skip_memory_write on/off).
  - `memory_write_node` happy path produces `pending_memory` with the expected keys.
  - Summarizer exhaustion → template fallback.
  - Embedding `None` → `content_vec` is `None` in `pending_memory`.
  - `memory_repository.upsert_memory_entry` insert vs update; `trim_oldest` evicts oldest-first.
- **Executor tests:** graph topology with memory enabled vs disabled; a pause mid-graph does not traverse `memory_write`.
- **Commit integration tests** (against the test DB):
  - Full successful-task flow writes exactly one row; follow-up overwrites; redrive overwrites.
  - Trim fires on the INSERT branch at `max_entries` boundary; does not fire on UPDATE branch.
  - Lease-revoked commit rolls back both the memory UPSERT and the task UPDATE.
- **Budget interaction tests:** `memory_write` super-step cost does not trigger per-task pause; the same cost on a different node would pause.
- **Regression:** all existing worker tests pass; no performance regression on memory-disabled runs.

## Constraints and Guardrails

- Do not introduce a new task status or a new pause state. The terminal transition is still `running → completed`.
- Do not write to any other tables besides `agent_memory_entries`, `tasks`, and `agent_cost_ledger` from the commit path.
- Do not put DB writes in the `memory_write` node. The design explicitly keeps those in the worker's post-astream path.
- Do not add an async background task or a secondary queue to recover from embedding outages. `content_vec = NULL` is the final state until a future backfill tool.
- Do not change the graph shape for memory-disabled agents. Do not add an empty no-op `memory_write` node that short-circuits — gate the edge addition itself.
- Do not implement the dead-letter hook, follow-up seeding, or attached-memory injection here — those are Task 8.
- Do not register memory tools — that is Task 7.
- Do not commit a memory row for HITL pauses, budget pauses, or any non-terminal state.
- Do not use the Track 3 per-step cost enforcement for memory — carve out by node name specifically.

## Assumptions

- Task 2 has shipped — `agent_config.memory.{enabled, summarizer_model, max_entries}` is readable from the snapshotted config the worker already loads.
- Task 5 has shipped — `compute_embedding(text) -> EmbeddingResult | None` is importable from `executor.embeddings`.
- Task 1 has shipped — `agent_memory_entries` exists with the UPSERT-friendly shape and `UNIQUE (task_id)`.
- `tasks.skip_memory_write` exists (see Task 4 coordination with Task 1). If it landed as a JSONB key instead, adjust the gate predicate accordingly.
- The existing `_handle_dead_letter` method handles the failure path unchanged in this task; Task 8 adds the memory branch there.
- Reaper re-claim and checkpoint resume already work for any LangGraph node — no special handling for the new node.

<!-- AGENT_TASK_END: task-6-worker-memory-write.md -->
