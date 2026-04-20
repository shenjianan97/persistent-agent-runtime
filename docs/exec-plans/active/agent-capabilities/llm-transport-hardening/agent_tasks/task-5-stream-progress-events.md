<!-- AGENT_TASK_START: task-5-stream-progress-events.md -->

# Task 5 — Conversation-Log Progress Events During Streaming

## Agent Instructions

You are a software engineer implementing one module of a larger system. Your scope is strictly limited to this task.

**CRITICAL PRE-WORK:** Before beginning implementation, you MUST read:
1. `docs/exec-plans/active/agent-capabilities/llm-transport-hardening/plan.md` — overall architecture.
2. Issue [#85](https://github.com/shenjianan97/persistent-agent-runtime/issues/85) — operator UX for long-running calls.
3. `services/worker-service/core/conversation_log_repository.py` — `append_entry` signature, `_VALID_KINDS`, `ConversationLogKind` literal, idempotency-key contract.
4. `infrastructure/database/migrations/0017_task_conversation_log.sql` lines 60–71 — the `chk_task_conversation_log_kind` CHECK constraint. This task **must** ship a migration to extend it; inserts of unknown kinds are rejected at the DB level regardless of what Python's `_VALID_KINDS` allows.
5. `services/worker-service/executor/graph.py` lines 1170–1210 — Task 4's streaming loop (already in place when this task starts). The progress-event hook lives inside that loop.
6. Existing `_convlog_append_*` helpers in `executor/graph.py` — pattern for assembling the `content` dict for an entry. Note actual kind names in use today are `user_turn`, `agent_turn`, `tool_call`, `tool_result`, `compaction_boundary`, `memory_flush`, `hitl_pause`, `hitl_resume` — match this naming scheme.
7. Track 7 Task 13 spec (`docs/exec-plans/completed/phase-2/track-7/agent_tasks/task-13-user-facing-conversation-log.md`) — establishes how operators consume the conversation log.
8. An existing migration that uses the DROP CONSTRAINT + ADD CONSTRAINT pattern for a CHECK constraint — for example `infrastructure/database/migrations/0010_sandbox_support.sql` (dead-letter reason allowlist extension). Postgres CHECK constraints are not ALTER-able in place; you must drop and re-add.

**CRITICAL POST-WORK:** After completing this task:
1. Apply the migration locally (`make db-migrate` or the project's equivalent — locate via `make help`) and verify the CHECK constraint accepts inserts of both new kinds (smoke test from a Python REPL using the worker venv against the dev DB at port 55432).
2. Run `services/worker-service/.venv/bin/python -m pytest services/worker-service/tests/test_conversation_log_repository.py services/worker-service/tests/test_graph_streaming.py -v`.
3. Run `make e2e-test` so the migration is exercised against a fresh `par-e2e-postgres` schema.
4. Manually drive a long-running task and inspect the conversation_log entries via the API (`GET /v1/tasks/{id}/conversation-log` or equivalent — locate the route).
5. Update `progress.md` row 5 to "Done".

## Context

Task 4 makes the streaming work but leaves the progress invisible to operators. The conversation log is the right channel for liveness — it's already SSE-streamed to the Console, indexed by checkpoint, and idempotency-protected against retries.

Two new entry kinds:

- `llm_stream_progress` — recurring during a stream, throttled. Carries cumulative chunk count, char count, tool-use char count, and elapsed seconds since the LLM call started.
- `llm_stream_complete` — exactly one per LLM call, on success. Carries the final totals so the Console can collapse the rolling progress entries into a single completion marker.

Throttling matters: a fast model emitting 200 chunks in 2 s would otherwise produce 200 conversation_log rows per LLM call. **First emission** happens on the first chunk arrival (typically <1 s after `astream` is called) so operators see liveness immediately. **Subsequent emissions** are throttled to one every 10 s wall time. **Terminal emission** is exactly one `llm_stream_complete` on success. A call that completes in <10 s therefore produces 1 progress + 1 complete entry — never zero progress entries.

## Task-Specific Shared Contract

- **New `ConversationLogKind` literals** in `core/conversation_log_repository.py`:
  - `"llm_stream_progress"` — `role` is `"assistant"`. `content` shape:
    ```text
    {
      "chunks": int,
      "chars_text": int,
      "chars_tool_call": int,
      "elapsed_s": float,
      "model": str,
    }
    ```
  - `"llm_stream_complete"` — `role` is `"assistant"`. `content` shape:
    ```text
    {
      "chunks": int,
      "chars_text": int,
      "chars_tool_call": int,
      "elapsed_s": float,
      "model": str,
      "stop_reason": str | None,
      "input_tokens": int | None,
      "output_tokens": int | None,
    }
    ```
- Both kinds are added to `_VALID_KINDS` **and** to the DB CHECK constraint via the new migration. Python and DB allowlists must stay in sync — drift causes silent insert failures.
- **DB migration** (`infrastructure/database/migrations/0018_conversation_log_streaming_kinds.sql`):
  - `ALTER TABLE task_conversation_log DROP CONSTRAINT chk_task_conversation_log_kind;`
  - `ALTER TABLE task_conversation_log ADD CONSTRAINT chk_task_conversation_log_kind CHECK (kind IN (...));` — list MUST contain every kind currently in production (`user_turn`, `agent_turn`, `tool_call`, `tool_result`, `system_note`, `compaction_boundary`, `memory_flush`, `hitl_pause`, `hitl_resume`) PLUS the two new ones. Re-read `0017_task_conversation_log.sql:60-71` immediately before writing the migration to confirm no other kinds were added by an intervening migration.
  - Filename uses the next free 4-digit prefix at migration-write time. `0018` is illustrative; verify by listing `infrastructure/database/migrations/` first.
- **Idempotency-key format:**
  - Progress: `f"{checkpoint_id}:stream_progress:{seq}"` where `seq` is a per-call monotonic counter starting at 1.
  - Complete: `f"{checkpoint_id}:stream_complete"`.
  - Both formats avoid colliding with Task 4's existing `agent_turn` idempotency keys.
- **Emission cadence:**
  - **First chunk → emit progress immediately** (`seq=1`, `elapsed_s` will be small — proves liveness).
  - **Subsequent chunks → emit progress when `now - last_emit_t >= 10.0` seconds.** Increment `seq` per emission.
  - **On normal completion → emit one `llm_stream_complete`.**
  - **On error / cancellation → no `llm_stream_complete`** (the existing post-call_error path covers the failure).
- **Non-fatal:** progress emission failures must not interrupt the stream. Use the existing `append_entry` "never raises" contract (returns `None` on DB error, logs internally).

## Affected Component

- **Service/Module:** Worker — Executor + Conversation log + DB schema
- **File paths:**
  - `infrastructure/database/migrations/00NN_conversation_log_streaming_kinds.sql` (new — `NN` is the next free prefix)
  - `services/worker-service/core/conversation_log_repository.py` (modify — add kinds to `_VALID_KINDS` and to the `ConversationLogKind` Literal)
  - `services/worker-service/executor/graph.py` (modify — add `_convlog_append_stream_progress` / `_convlog_append_stream_complete` helpers; emit from Task 4's streaming loop)
  - `services/worker-service/tests/test_graph_streaming.py` (extend Task 4's tests)
  - `services/worker-service/tests/test_conversation_log_repository.py` (extend — assert new kinds round-trip via `append_entry` AND survive a real DB insert against the migrated schema)
- **Change type:** new migration + modification + test extensions

## Dependencies

- **Must complete first:** Task 4 (streaming loop). The throttle logic and chunk counters live inside that loop.
- **Provides output to:** Task 6 (Console renders these entries).
- **Shared interfaces/contracts:** the two new kinds and their content shapes are the contract Task 6 reads.

## Implementation Specification

### Migration

Create `infrastructure/database/migrations/00NN_conversation_log_streaming_kinds.sql` (where `NN` is the next free 4-digit prefix when this task is implemented). The migration must:

1. `DROP CONSTRAINT chk_task_conversation_log_kind` on `task_conversation_log`.
2. `ADD CONSTRAINT chk_task_conversation_log_kind CHECK (kind IN (...))` with the *full* list — every kind currently in production plus `llm_stream_progress` and `llm_stream_complete`.

Confirm the live list against `0017_task_conversation_log.sql:60-71` immediately before writing — do not paste from memory.

### `core/conversation_log_repository.py`

Add `"llm_stream_progress"` and `"llm_stream_complete"` to `_VALID_KINDS` and to `ConversationLogKind`'s `Literal[...]`. No other changes.

### `executor/graph.py`

Two new helper functions next to the existing `_convlog_append_llm_response`:

- `_convlog_append_stream_progress(repo, *, task_id, tenant_id, checkpoint_id, seq, content)` — wraps `append_entry` with kind `"llm_stream_progress"` and idempotency-key `f"{checkpoint_id}:stream_progress:{seq}"`.
- `_convlog_append_stream_complete(repo, *, task_id, tenant_id, checkpoint_id, content)` — wraps `append_entry` with kind `"llm_stream_complete"` and idempotency-key `f"{checkpoint_id}:stream_complete"`.

Inside Task 4's streaming loop:

- Initialize `_stream_t0 = time.monotonic()`, `_last_emit_t = _stream_t0`, `_emit_seq = 0`, `_chunks = 0`, `_chars_text = 0`, `_chars_tool = 0`.
- After each `chunk` arrives:
  - Update counters (`_chunks += 1`; for `_chars_text`, sum `len(chunk.content)` if string; for `_chars_tool`, sum lengths of any `tool_call_chunks` partials).
  - **Emit if any of the following:**
    - `_emit_seq == 0` — first chunk; emit immediately to prove liveness.
    - `time.monotonic() - _last_emit_t >= 10.0` — throttle window elapsed.
  - On emit:
    - `_emit_seq += 1`
    - Build the `content` dict per the contract above.
    - Call `_convlog_append_stream_progress(...)`.
    - `_last_emit_t = time.monotonic()`.
- After the loop completes successfully, build the `llm_stream_complete` content (including final usage from `final_message.usage_metadata` and `stopReason` from `response_metadata`) and call `_convlog_append_stream_complete(...)`.
- On exception (rate-limit raise, cancellation): no terminal `llm_stream_complete` entry is emitted (the existing post-call_error path covers the failure). Progress entries already emitted remain.

### Tests

Extend `tests/test_graph_streaming.py`:

- Stub LLM that streams 50 chunks over 30 s with controlled timing. Assert exactly 4 progress entries: 1 at first chunk + 3 throttled emissions at the 10/20/30 s boundaries (give or take one tick depending on chunk arrival times).
- Stream that completes in 2 s emits **exactly one** progress entry (the first-chunk one) and exactly one `llm_stream_complete`.
- Stream that fails mid-way still emits the first-chunk progress entry, plus any throttled emissions before failure, but no `llm_stream_complete` (existing error path unchanged).
- Idempotency-key collision: replay the same checkpoint and assert no duplicate rows.

Extend `tests/test_conversation_log_repository.py`:

- Both new kinds round-trip via `append_entry` and `fetch_*` (whichever read API exists).
- **Integration test**: insert one row of each new kind against the migrated test DB (`par-e2e-postgres` on port 55433) and confirm the CHECK constraint accepts it. Without this test the migration could silently regress — e.g., a future migration drops and re-adds the constraint without including the new kinds.

## Acceptance Criteria

- [ ] Migration extends `chk_task_conversation_log_kind` with both new kinds and ships in the next free migration prefix slot. CI's migration glob picks it up automatically.
- [ ] `_VALID_KINDS` contains the two new literals; `ConversationLogKind` Literal is updated.
- [ ] **First chunk emits a progress entry** (proven by a unit test that asserts the first emission's `seq == 1` regardless of stream length).
- [ ] During a streaming call ≥ 30 s, the conversation log gains ≥ 4 `llm_stream_progress` entries (1 at first chunk + ≥ 3 throttled) plus 1 `llm_stream_complete`.
- [ ] During a streaming call <10 s, the conversation log gains exactly 1 `llm_stream_progress` (the first-chunk one) plus 1 `llm_stream_complete` — never zero progress entries.
- [ ] Idempotency keys prevent duplicates on checkpoint replay.
- [ ] Progress emission failures do not interrupt the LLM call.
- [ ] Integration test inserts one row of each new kind against `par-e2e-postgres` after migration and asserts the CHECK constraint accepts both.
- [ ] All extended tests pass.

## Out of Scope

- Console rendering (Task 6).
- Surfacing progress via a separate SSE channel — reuses the existing conversation log SSE.

<!-- AGENT_TASK_END -->
