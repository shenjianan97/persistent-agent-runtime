<!-- AGENT_TASK_START: task-8-worker-deadletter-followup-attach.md -->

# Task 8 — Dead-Letter Hook, Follow-Up Seeding, and Attached-Memory Injection

## Agent Instructions

You are a software engineer implementing one module of a larger system. Your scope is strictly limited to this task.

**CRITICAL PRE-WORK:** Before beginning implementation, you MUST read:
1. `docs/design-docs/phase-2/track-5-memory.md` — sections "Dead-letter hook (failed tasks with observations)", "Follow-up seeding" (in "Mid-task writes"), "Read Path → Retrieval is always explicit" (item 1 describing attach injection), and "Validation and Consistency Rules".
2. `services/worker-service/executor/graph.py` — the existing `_handle_dead_letter` method and the post-astream commit path (from Task 6).
3. `services/worker-service/core/worker.py` — the lease-validated task-state transitions.
4. `services/worker-service/checkpointer/` — how `aget_tuple(thread_id)` retrieves the last checkpoint + graph state.
5. Task 6's output — `MemoryEnabledState`, `memory_repository.upsert_memory_entry`, the successful-commit transaction pattern.
6. Task 7's output — `observations` is populated by `memory_note`; the dead-letter hook reads from the same state field.
7. Task 4's output — `task_attached_memories` rows exist at task submission time; the worker reads these at task start to inject resolved entries into the initial prompt.

**CRITICAL POST-WORK:** After completing this task:
1. Run `make worker-test` and `make e2e-test`. Fix any regressions.
2. Update the status in `docs/exec-plans/active/phase-2/track-5/progress.md` to "Done".

## Context

Task 8 closes three remaining gaps in the worker:

1. **Dead-letter memory hook.** When a task reaches `dead_letter`, a memory entry is written only if (a) `dead_letter_reason != 'cancelled_by_user'` AND (b) at least one observation exists. The entry is template-only — no summarizer LLM call. This is distinct from the successful path (Task 6), and branches from `_handle_dead_letter` inside the same lease-validated transaction as the `UPDATE tasks SET status='dead_letter'`.
2. **Follow-up / redrive observation seeding.** Because follow-up and redrive reuse the same `task_id` and UPSERT the memory row, the original observations would be lost if the fresh execution started from an empty `observations` list. Before the graph begins running, the worker seeds `MemoryEnabledState.observations` from the existing row's `observations` column (if any). This preserves first-execution observations across follow-up / redrive.
3. **Attached-memory injection at task start.** When the task has rows in `task_attached_memories`, the worker resolves those to full memory entries (`title + observations + summary`), formats them as a prompt prefix, and injects them into the graph's initial message list. This is the "customer attach at submission" retrieval path.

All three branches are gated on `effective_memory_enabled` for the writes (dead-letter, seeding). The attach-injection path runs regardless of `effective_memory_enabled` — even a memory-disabled agent can receive attached memories at submission (the Console gates this at the UI layer when the agent has memory disabled; the worker does not additionally gate it).

## Task-Specific Shared Contract

- **Dead-letter hook ordering (critical):**
  ```
  BEGIN;
    -- 1) Read last checkpoint (for observations) via checkpointer cursor.
    -- 2) If dead_letter_reason == 'cancelled_by_user' → skip memory write.
    -- 3) Else if observations empty → skip memory write.
    -- 4) Else:
    --    - Build template title:   "[Failed] <first 50 chars of task_input>"
    --    - Build template summary: "Task dead-lettered after <retries> retries: <last_error_code> — <last_error_message>"
    --    - outcome = 'failed'
    --    - summarizer_model_id = 'template:dead_letter'
    --    - compute_embedding(concatenated text)  -- may return None, handled downstream
    --    - UPSERT into agent_memory_entries (uses same repository helper as Task 6).
    -- 5) UPDATE tasks SET status='dead_letter', dead_letter_reason=… WHERE task_id=:id AND lease_owner=:me;
  COMMIT;
  ```
  - No LLM call on this path — template only.
  - Embedding call (if provider up) happens inside the transaction's scope. If provider is down, `content_vec = NULL`; row is still written. Preserve the invariant: observations-bearing genuine failures produce a row.
  - Lease validation on step 5 rolls back everything if the worker lost the lease.
  - FIFO trim applies here identically to the successful path (INSERT branch only, same repository helper).
- **`cancelled_by_user` is a `dead_letter_reason` in Phase 2 Track 2**, NOT a distinct task status. Do not introduce any new enum values.
- **Follow-up / redrive seeding:**
  - Both follow-up and redrive reuse the same `task_id`.
  - Before `astream` begins, if `effective_memory_enabled`:
    - Query `agent_memory_entries` by `task_id` (scoped by tenant_id + agent_id).
    - If a row exists, seed `MemoryEnabledState.observations` from `observations` column (verbatim, in order).
    - If no row exists, start with an empty list.
  - Seeding runs via a one-off state update before `astream` — do NOT hand-edit the checkpoint rows in place.
  - Redrive with `rollback_last_checkpoint` (a Phase 2 feature) still seeds observations — the rollback only rewinds the LangGraph state, not the memory row; the two are independent.
- **Attached-memory injection:**
  - At the top of `execute_task`, before graph assembly, resolve `task_attached_memories` rows for the current `task_id` via a single scoped query joining `agent_memory_entries`:
    ```
    SELECT tam.position, am.title, am.summary, am.observations
    FROM task_attached_memories tam
    LEFT JOIN agent_memory_entries am
      ON am.memory_id = tam.memory_id
     AND am.tenant_id = :tenant
     AND am.agent_id = :agent
    WHERE tam.task_id = :task
    ORDER BY tam.position;
    ```
  - Silently skip any row where the LEFT JOIN did not find a memory entry (it was deleted post-submission) — per design doc, the attachment record is preserved, but the injection content is just whatever still resolves.
  - Format each resolved entry as a system-prompt-prefix block:
    ```
    [Attached memory: <title>]
    Observations:
    - <obs 1>
    - <obs 2>
    Summary: <summary>
    ```
  - Concatenate blocks (in `position` order) and prepend as a `SystemMessage` BEFORE the agent's system prompt in the initial messages list. If no attached memories resolve, no prefix is added — the initial message list is unchanged.
  - Emit `memory.attach.injected` structured log with the count of resolved entries and total prefix byte size (so operations can detect over-attach at scale).
  - If the task is a follow-up (i.e., continues an already-running conversation), the attachments were injected on the first execution — DO NOT re-inject. Determine "first execution" by the presence of any existing conversation history in the checkpoint. The rule: injection happens once, when the graph starts from an empty message list. (The design doc's "immutable after task creation" attachment rule makes this correct — follow-up runs already include the first-execution messages, which already contain the injected prefix.)

## Affected Component

- **Service/Module:** Worker Service — Executor and Post-Astream Commit
- **File paths:**
  - `services/worker-service/executor/graph.py` (modify — `_handle_dead_letter` gains the memory branch; `execute_task` gains seeding + attach-injection at the top)
  - `services/worker-service/executor/memory_graph.py` (modify — add `build_pending_memory_dead_letter_template`, `seed_observations_from_existing_row`)
  - `services/worker-service/core/memory_repository.py` (modify — add `read_memory_observations_by_task_id`, `resolve_attached_memories_for_task`)
  - `services/worker-service/tests/test_memory_dead_letter.py` (new)
  - `services/worker-service/tests/test_memory_attach_injection.py` (new)
  - `services/worker-service/tests/test_memory_follow_up_seeding.py` (new)
- **Change type:** modification + new code

## Dependencies

- **Must complete first:** Task 4 (task submission persists `task_attached_memories`), Task 6 (successful-path commit + memory repository helpers), **Task 7 (data-flow)** — the dead-letter hook reads the `observations` that `memory_note` (Task 7) populates. Task 8 can LAND without Task 7 (the hook gracefully handles an empty `observations` list), but its end-to-end tests require Task 7 to produce non-empty observations.
- **Provides output to:** Task 11 (E2E).
- **Shared interfaces/contracts:** The three worker-side behaviours — dead-letter memory hook, follow-up seeding, attach injection.
- **Parallel-safety:** Tasks 6 and 7 both edit `services/worker-service/executor/graph.py`. If dispatched concurrently, use `isolation: "worktree"` on one or more agents and merge on completion per AGENTS.md §Parallel Subagent Safety.

## Implementation Specification

### Dead-letter memory hook

- Extend `_handle_dead_letter` with a memory branch placed BEFORE the existing `UPDATE tasks SET status='dead_letter' …` but inside the same transaction. If `_handle_dead_letter` today runs the UPDATE outside a transaction / via a single statement, refactor it to open a transaction that covers both the memory UPSERT (conditional) and the task UPDATE.
- Guard the memory branch with `effective_memory_enabled` AND the two skip conditions (`cancelled_by_user` OR empty observations).
- On the template fallback, fields populated per the design doc exactly. `summarizer_model_id = 'template:dead_letter'`. `outcome = 'failed'`. `observations` preserved verbatim.
- Reuse `memory_repository.upsert_memory_entry` (same helper as Task 6) — no divergent code path.
- Embedding computation uses `compute_embedding(text)` with the concatenated title + summary + observations + tags (tags is `[]`). Returns `None` handled as `content_vec = NULL`.
- After the task UPDATE, emit `memory.deadletter.template` structured log (tenant, agent, task, reason, observation_count).

### Follow-up / redrive observation seeding

- At the top of `execute_task`, after `effective_memory_enabled` is computed and before the graph runs:
  - If `effective_memory_enabled`:
    - Call `memory_repository.read_memory_observations_by_task_id(conn, tenant, agent, task_id) -> list[str] | None`.
    - If non-None, inject as the initial value of the `observations` state field via the graph's input `state` argument (LangGraph accepts initial state on invocation). Do NOT mutate the checkpoint rows directly.
    - Log `memory.seeding.applied` with the observation count.
- The helper should return `None` when no memory row exists (first-run task), empty list when the row exists with empty observations, and a list when observations were recorded in a prior execution.

### Attached-memory injection at task start

- Also at the top of `execute_task`:
  - Detect "first execution" by checking whether the checkpointer has any existing history for this `thread_id`. If history exists, skip injection (follow-up).
  - Query `memory_repository.resolve_attached_memories_for_task(conn, tenant, agent, task_id) -> list[{position, title, summary, observations}]`.
  - If the list is non-empty, build the prompt-prefix `SystemMessage` and prepend it to the initial messages list.
  - Emit `memory.attach.injected` with `{count, approx_bytes}`.

### Detection-of-first-execution contract

- Use the checkpointer's `aget_tuple(thread_id)` — if it returns `None` OR a tuple whose `values` map lacks any `messages` key OR a tuple whose message list is empty, it is a first run.
- Document this single "first-run" predicate as a small helper in `memory_graph.py` so both the seeding-skip logic and the injection-skip logic share it.
- **Robustness:** LangGraph's durability modes can, in principle, persist an empty initial state before the first super-step. Task 11's E2E suite MUST include a test that rapid-pauses-and-resumes a task between the initial state commit and the first super-step, then asserts the injection does NOT re-run. If that test flakes, fall back to a stronger predicate: check `count(*)` on the checkpointer's history table for the `thread_id` and require count > 1 to treat as follow-up.

## Acceptance Criteria

- [ ] Dead-letter of a task with `dead_letter_reason = 'cancelled_by_user'` writes NO memory row, regardless of observations.
- [ ] Dead-letter of a task with no observations AND any other reason writes NO memory row.
- [ ] Dead-letter of a task with observations AND a non-cancelled reason writes exactly one row with `outcome='failed'`, `summarizer_model_id='template:dead_letter'`, observations preserved, `tags=[]`.
- [ ] The dead-letter memory write + `UPDATE tasks SET status='dead_letter'` are atomic — if the `UPDATE` fails lease validation, the memory UPSERT rolls back.
- [ ] Embedding-provider failure on the dead-letter path writes the row with `content_vec = NULL`. The task still transitions to `dead_letter`.
- [ ] Follow-up / redrive execution starts with `observations` seeded from the existing memory row; first-time execution starts with an empty `observations` list.
- [ ] Follow-up execution does NOT re-inject attached memories. First-time execution with `task_attached_memories` DOES inject them as a system-prompt-prefix.
- [ ] Attached-memory injection skips memory ids that no longer resolve (post-delete). The injected prefix contains only entries that still exist.
- [ ] Attached-memory injection emits a single structured log line with count and byte size.
- [ ] Tasks with no attachments have an unchanged initial message list (no empty prefix, no system message).
- [ ] Cross-agent or cross-tenant `memory_id`s never resolve — the scoped LEFT JOIN skips them silently.
- [ ] On the dead-letter template-write path, a successful `compute_embedding` call writes an `agent_cost_ledger` row attributed to the task's current checkpoint id. A failed `compute_embedding` writes no ledger row but the memory row is still written with `content_vec = NULL`.
- [ ] `make worker-test` and `make e2e-test` pass.

## Testing Requirements

- **Dead-letter tests** (`test_memory_dead_letter.py`):
  - Cancelled by user → no row written; observations retained in checkpoint for forensic access via `task_history_get`.
  - Genuine failure, no observations → no row written.
  - Genuine failure, with observations → template row written, `summarizer_model_id='template:dead_letter'`.
  - Lease revoked mid-transaction → no row AND no `dead_letter` status change.
  - Embedding down → row written with `content_vec = NULL`.
- **Follow-up seeding tests** (`test_memory_follow_up_seeding.py`):
  - First run: seeding returns `None`; graph starts with empty observations.
  - Second run on same `task_id` after a successful memory write: seeding returns the prior observations; graph starts with them populated.
  - Redrive after dead-letter: seeding returns observations from the previous (template) memory row.
- **Attach injection tests** (`test_memory_attach_injection.py`):
  - First-run task with two attached entries: initial messages contain a prepended SystemMessage whose content includes both entries' titles and summaries, in `position` order.
  - Follow-up task: no re-injection; initial messages equal the checkpointed history.
  - Attached entry that was deleted post-submission: silently omitted from the prefix.
  - Cross-tenant / cross-agent memory id in the attachment table (shouldn't happen but test defensively): silently omitted.
- **Regression:** all existing worker tests pass.

## Constraints and Guardrails

- Do not introduce a new `dead_letter_reason` value. `cancelled_by_user` is the one canonical marker.
- Do not perform an LLM call on the dead-letter path. Template-only.
- Do not rewrite `task_attached_memories` on follow-up / redrive.
- Do not inject resolved attached memories via the customer's system prompt — prepend a separate SystemMessage so the distinction is preserved.
- Do not cache the injected prefix in the task row. The resolution runs once at task start; subsequent runs already have it in the checkpoint.
- Do not emit observation contents in structured log lines — count + byte size only.
- Do not short-circuit the seeding lookup when `max_entries` has trimmed the task's own memory row (it cannot — the row exists only for the same task_id; trim removes OLDER rows). If the row is missing, the task was simply never memory-written before.
- Do not make the attached-memory injection skip follow-up execution by rewriting messages — the checkpoint already contains them; adjusting the initial-state input is sufficient.

## Assumptions

- Task 4 has shipped — `task_attached_memories` rows exist for tasks submitted with `attached_memory_ids`.
- Task 6 has shipped — `memory_repository.upsert_memory_entry` and the lease-validated transaction pattern are available.
- Task 7 has shipped — `memory_note` populates `observations` via the `operator.add` reducer; the dead-letter hook reads the already-committed `observations` from the final checkpoint state.
- The checkpointer supports `aget_tuple(thread_id)`; the "first execution" predicate is derivable from its output.
- `tasks` has columns `retry_count` and `last_error_code` / `last_error_message` (from Phase 1) — the template summary uses those.

<!-- AGENT_TASK_END: task-8-worker-deadletter-followup-attach.md -->
