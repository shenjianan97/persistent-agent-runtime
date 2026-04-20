<!-- AGENT_TASK_START: task-5-stream-progress-events.md -->

# Task 5 — Conversation-Log Progress Events During Streaming

## Agent Instructions

You are a software engineer implementing one module of a larger system. Your scope is strictly limited to this task.

**CRITICAL PRE-WORK:** Before beginning implementation, you MUST read:
1. `docs/exec-plans/active/agent-capabilities/llm-transport-hardening/plan.md` — overall architecture.
2. Issue [#85](https://github.com/shenjianan97/persistent-agent-runtime/issues/85) — operator UX for long-running calls.
3. `services/worker-service/core/conversation_log_repository.py` — `append_entry` signature, `_VALID_KINDS`, `ConversationLogKind` literal, idempotency-key contract.
4. `services/worker-service/executor/graph.py` lines 1170–1210 — Task 4's streaming loop (already in place when this task starts). The progress-event hook lives inside that loop.
5. Existing `_convlog_append_*` helpers in `executor/graph.py` — pattern for assembling the `content` dict for an entry.
6. Track 7 Task 13 spec (`docs/exec-plans/completed/phase-2/track-7/agent_tasks/task-13-user-facing-conversation-log.md`) — establishes how operators consume the conversation log.

**CRITICAL POST-WORK:** After completing this task:
1. Run `services/worker-service/.venv/bin/python -m pytest services/worker-service/tests/test_conversation_log_repository.py services/worker-service/tests/test_graph_streaming.py -v`.
2. Manually drive a long-running task and inspect the conversation_log entries via the API (`GET /v1/tasks/{id}/conversation-log` or equivalent — locate the route).
3. Update `progress.md` row 5 to "Done".

## Context

Task 4 makes the streaming work but leaves the progress invisible to operators. The conversation log is the right channel for liveness — it's already SSE-streamed to the Console, indexed by checkpoint, and idempotency-protected against retries.

Two new entry kinds:

- `llm_stream_progress` — recurring during a stream, throttled. Carries cumulative chunk count, char count, tool-use char count, and elapsed seconds since the LLM call started.
- `llm_stream_complete` — exactly one per LLM call, on success. Carries the final totals so the Console can collapse the rolling progress entries into a single completion marker.

Throttling matters: a fast model emitting 200 chunks in 2 s would otherwise produce 200 conversation_log rows per LLM call. Default throttle: emit at most one `llm_stream_progress` every 10 s of wall time.

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
- Both kinds are added to `_VALID_KINDS`. **No DB migration** — `_VALID_KINDS` is enforced in Python only (verify by reading the file).
- **Idempotency-key format:**
  - Progress: `f"{checkpoint_id}:stream_progress:{seq}"` where `seq` is a per-call monotonic counter starting at 1.
  - Complete: `f"{checkpoint_id}:stream_complete"`.
  - Both formats avoid colliding with Task 4's existing `llm_response` idempotency keys.
- **Throttling:** progress entries are emitted when (`now - last_progress_emit_at >= 10.0` seconds) **AND** (`chunks - last_emitted_chunks >= 1`). The first chunk does not emit a progress entry; the first emission is at the 10 s mark or the natural completion, whichever is first.
- **Non-fatal:** progress emission failures must not interrupt the stream. Use the existing `append_entry` "never raises" contract (returns `None` on DB error, logs internally).

## Affected Component

- **Service/Module:** Worker — Executor + Conversation log
- **File paths:**
  - `services/worker-service/core/conversation_log_repository.py` (modify — add kinds to `_VALID_KINDS`)
  - `services/worker-service/executor/graph.py` (modify — add `_convlog_append_stream_progress` / `_convlog_append_stream_complete` helpers; emit from Task 4's streaming loop)
  - `services/worker-service/tests/test_graph_streaming.py` (extend Task 4's tests)
  - `services/worker-service/tests/test_conversation_log_repository.py` (extend — assert new kinds round-trip)
- **Change type:** modification + test extensions

## Dependencies

- **Must complete first:** Task 4 (streaming loop). The throttle logic and chunk counters live inside that loop.
- **Provides output to:** Task 6 (Console renders these entries).
- **Shared interfaces/contracts:** the two new kinds and their content shapes are the contract Task 6 reads.

## Implementation Specification

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
  - If `time.monotonic() - _last_emit_t >= 10.0`:
    - `_emit_seq += 1`
    - Build the `content` dict per the contract above.
    - Call `_convlog_append_stream_progress(...)`.
    - `_last_emit_t = time.monotonic()`.
- After the loop completes successfully, build the `llm_stream_complete` content (including final usage from `final_message.usage_metadata` and `stopReason` from `response_metadata`) and call `_convlog_append_stream_complete(...)`.
- On exception (rate-limit raise, cancellation): no terminal `llm_stream_complete` entry is emitted (the existing post-call_error path covers the failure). Progress entries already emitted remain.

### Tests

Extend `tests/test_graph_streaming.py`:

- Stub LLM that streams 50 chunks with controlled timing. Assert exactly N progress entries are emitted at the 10-s throttle boundary.
- Stream that completes in under 10 s emits **zero** progress entries and exactly one `llm_stream_complete`.
- Stream that fails mid-way emits zero `llm_stream_complete` (existing error path unchanged).
- Idempotency-key collision: replay the same checkpoint and assert no duplicate rows.

Extend `tests/test_conversation_log_repository.py`:

- Both new kinds round-trip via `append_entry` and `fetch_*` (whichever read API exists).

## Acceptance Criteria

- [ ] `_VALID_KINDS` contains the two new literals.
- [ ] During a streaming call ≥ 30 s, the conversation log gains at least 2 `llm_stream_progress` entries plus 1 `llm_stream_complete`.
- [ ] Idempotency keys prevent duplicates on checkpoint replay.
- [ ] Progress emission failures do not interrupt the LLM call.
- [ ] All extended tests pass.

## Out of Scope

- Console rendering (Task 6).
- Surfacing progress via a separate SSE channel — reuses the existing conversation log SSE.

<!-- AGENT_TASK_END -->
