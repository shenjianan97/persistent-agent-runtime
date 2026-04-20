<!-- AGENT_TASK_START: task-4-streaming-llm-call.md -->

# Task 4 — Switch the LLM Call to Streaming (`astream`)

## Agent Instructions

You are a software engineer implementing one module of a larger system. Your scope is strictly limited to this task.

**CRITICAL PRE-WORK:** Before beginning implementation, you MUST read:
1. `docs/exec-plans/active/agent-capabilities/llm-transport-hardening/plan.md` — overall architecture.
2. Issue [#85](https://github.com/shenjianan97/persistent-agent-runtime/issues/85) — root cause: `ainvoke` blocks until full response; the worker has no progress signal during a long generation.
3. `services/worker-service/executor/graph.py` lines **1060–1210** — the `agent_node` body around the LLM call. Pay particular attention to:
   - The compaction pre-call pipeline (`compact_for_llm`) — runs unchanged.
   - The rate-limit retry loop (`for attempt in range(max_rate_limit_retries + 1)`) — preserve.
   - The conversation log append after a successful response (`_convlog_append_llm_response`) — preserve, but invoked with the materialized `AIMessage`, not chunks.
   - The `Command(update=...)` return shape — preserve.
4. LangChain's `AIMessageChunk` documentation:
   - Chunks merge via `+` operator (e.g., `merged = chunks[0]; for c in chunks[1:]: merged = merged + c`).
   - Tool calls accumulate via `tool_call_chunks` field, materialized into `tool_calls` on the merged final.
   - `usage_metadata` and `response_metadata` (including `stopReason`) arrive only on the final chunk.
5. Existing examples in the codebase that already use streaming (search: `astream_events`, `astream`, `stream_mode`).

**CRITICAL POST-WORK:** After completing this task:
1. Run `services/worker-service/.venv/bin/python -m pytest services/worker-service/tests/test_graph_streaming.py services/worker-service/tests/test_executor.py -v`. The full executor test suite must remain green.
2. Manually run the repro task from #85 (smaller version is fine — any task that exercises `agent_node`) and verify worker logs show streaming progress.
3. Update `progress.md` row 4 to "Done".

## Context

The worker's LLM invocation lives in one place: `executor/graph.py:1170-1206`. Today it uses `llm_with_tools.ainvoke(messages_for_llm, config)`. With Bedrock + a slow marketplace model (GLM-5 at ~35 tok/s), a 7k-token tool-use generation takes ~200 s, but the worker sees nothing for those 200 s, then either succeeds or hits a `ReadTimeoutError`.

`astream` returns `AIMessageChunk` objects as the model produces them. botocore's `read_timeout` is **per-read**, so as long as bytes keep arriving the timeout does not trip — even a 4-min generation succeeds with the same 120 s read_timeout. The worker can also report progress (Task 5) by inspecting accumulated chunks.

The downstream contract — what the rest of the graph sees — must not change. After streaming completes, the agent_node returns `{"messages": [final_AIMessage], **compaction_state_updates}` exactly as today.

## Task-Specific Shared Contract

- **Replace** `await self._await_or_cancel(llm_with_tools.ainvoke(...))` with a streaming loop that:
  1. Calls `llm_with_tools.astream(messages_for_llm, config)`.
  2. Iterates over `AIMessageChunk` objects, accumulating into a `merged: AIMessageChunk | None` variable using `+`.
  3. Wraps the iteration in `self._await_or_cancel`-equivalent handling so cancellation still works (the existing helper may need to be adapted for an async iterator; if so, write a sibling helper `_astream_or_cancel`).
  4. After the loop, materialize `final_message = AIMessage(...)` from `merged` (LangChain provides a method to convert; if not, copy fields). Preserve `tool_calls`, `content`, `response_metadata`, `usage_metadata`.
  5. Hands `final_message` to the existing `_convlog_append_llm_response(...)` call and returns it inside the existing `Command` shape.
- **Preserve** the rate-limit retry loop. `astream` may raise the same exception classes as `ainvoke`; the existing `_is_rate_limit_error` check still applies. A streaming call that raises mid-stream is treated as a single failed attempt (not partially retried).
- **Surface `stopReason=max_tokens` as a structured warning.** When `final_message.response_metadata.get("stopReason") == "max_tokens"`, log a warning `llm.max_tokens_reached` with `task_id`, `model`, the configured `max_tokens` (read from `llm_with_tools` if accessible, else from agent_config), and the resulting message length. Do not raise — the AIMessage flows downstream as usual.
- **No new public API.** All changes are internal to `agent_node`. The streaming loop is implementation detail.
- **Cancellation must remain prompt.** A `cancel_event` that fires mid-stream must abort within ≤ 1 s. The simplest implementation: `await asyncio.wait_for(asyncio.shield(...), ...)` or wrap the iterator in a helper that polls `cancel_event` between chunks.

## Affected Component

- **Service/Module:** Worker — Executor
- **File paths:**
  - `services/worker-service/executor/graph.py` (modify lines 1170–1206; possibly add a small `_astream_or_cancel` helper next to `_await_or_cancel`)
  - `services/worker-service/tests/test_graph_streaming.py` (new)
- **Change type:** rewrite of one method body + new tests

## Dependencies

- **Must complete first:** Task 1 (resolver), Task 2 + 3 (`max_tokens` set on the model — required for the stopReason check).
- **Provides output to:** Task 5 (progress events read from the same chunk loop), Task 9 (repro test).
- **Shared interfaces/contracts:** internal to `agent_node`. No public surface change.

## Implementation Specification

### Streaming loop

The new body of the success branch in the rate-limit-retry loop:

1. Call `llm_with_tools.astream(messages_for_llm, config)` to get an async iterator of `AIMessageChunk`.
2. Iterate, merging via `+`. Track `chunks_received` (count) and `wall_elapsed_s` for Task 5's hook point.
3. On normal completion, materialize `final_message`.
4. If `final_message.response_metadata.get("stopReason") == "max_tokens"`, emit `logger.warning("llm.max_tokens_reached", extra={...})` with structured fields.
5. Append to conversation log (existing helper).
6. Return `Command(...)`.

### Cancellation helper

If the existing `_await_or_cancel` doesn't transparently handle async iterators, add a sibling helper:

```text
async def _astream_or_cancel(self, aiter, cancel_event, *, task_id, operation):
    """Yield from aiter, raising CancelledError as soon as cancel_event is set."""
```

The helper polls `cancel_event` between chunks (cheap; chunks arrive every 20–500 ms).

### Tests: `services/worker-service/tests/test_graph_streaming.py`

Cover:

- **Parity with `ainvoke`.** Given a stub LLM that, when streamed, yields a fixed sequence of `AIMessageChunk` objects, the merged final `AIMessage` is structurally equivalent to what `ainvoke` would have produced for the same input. Compare `content`, `tool_calls`, `response_metadata.stopReason`, `usage_metadata.input_tokens` and `output_tokens`.
- **`stopReason=max_tokens` surfaces a warning.** Stub returns chunks with `response_metadata={"stopReason": "max_tokens"}`. Assert `caplog` contains `llm.max_tokens_reached` with the expected structured fields. Assert the `Command` is still returned (no exception).
- **Cancellation mid-stream.** Stub yields chunks with `await asyncio.sleep(0.05)` between each. Set `cancel_event` after the second chunk. Assert the loop exits within 1 s with `asyncio.CancelledError`.
- **Tool-call accumulation.** Stub yields chunks containing `tool_call_chunks` that span multiple chunks (typical Bedrock streaming). Assert the merged final has `tool_calls` correctly assembled.
- **Rate-limit retry on streaming.** Stub raises a rate-limit exception on first attempt; succeeds on second. Assert the retry loop fires once, the second attempt's merged result is what's returned.

## Acceptance Criteria

- [ ] `agent_node` no longer calls `ainvoke`; uses `astream` with chunk merging.
- [ ] All existing executor unit and integration tests pass unchanged (no regression in the contract `agent_node` exposes to the rest of the graph).
- [ ] `services/worker-service/tests/test_graph_streaming.py` passes.
- [ ] `stopReason=max_tokens` produces a structured warning visible in worker logs.
- [ ] Cancellation aborts the stream within 1 s.
- [ ] Worker log on a real Bedrock call shows the LLM call completing (`TEMP_DEBUG_BEDROCK post_call_ok` for now; will become `llm_stream_complete` after Task 5) without `ReadTimeoutError` for a 60 s+ generation.

## Out of Scope

- Emitting per-chunk progress events to the conversation log (Task 5).
- Console rendering (Task 6).
- Removing `TEMP_DEBUG_BEDROCK` markers (Task 9).

<!-- AGENT_TASK_END -->
