<!-- AGENT_TASK_START: task-6-tier-3-summarizer.md -->

# Task 6 — Tier 3 Summarizer

## Agent Instructions

**CRITICAL PRE-WORK:**
1. `docs/design-docs/phase-2/track-7-context-window-management.md` — section "Tier 3: retrospective LLM summarization", plus "Budget interaction (Track 3)" and "Summarizer outage" sub-sections.
2. `services/worker-service/executor/graph.py` — `_build_summarizer_callable` from Track 5, which already wraps a cheap-model LLM call. Reuse the cost-ledger attribution, Langfuse callback, and retry-friendly shape.
3. `services/worker-service/core/cost_ledger_repository.py` — the insert path for `agent_cost_ledger` rows.
4. `services/worker-service/core/heartbeat.py` or similar — how the worker surfaces transient retries vs terminal failures.
5. Track 5 Task 6 (`task-6-worker-memory-write.md`) — precedent for cheap-summarizer LLM calls with cost-ledger attribution.

**CRITICAL POST-WORK:**
1. Run `make worker-test`. Integration-level tests from this task require a mocked LLM client; do not require real provider credentials.
2. Update Task 6 status in `docs/exec-plans/active/phase-2/track-7/progress.md`.

## Context

Tier 3 is the last-resort LLM summarization. When Tier 1 + 1.5 have run and the estimated input is still above the Tier 3 threshold, this module takes the slice `messages[summarized_through_turn_index : protect_from_index]` and produces a factual summary that replaces that prefix.

The output is **appended** to `state.summary_marker`, preserving the KV-cache prefix on the marker region across subsequent Tier 3 firings within the same task.

The summarizer runs with retries (`SUMMARIZER_MAX_RETRIES = 2`). After exhaustion, the function returns a `SummarizeResult` with `skipped=True` — callers do NOT treat this as a terminal error; the next agent-node call will re-attempt.

## Task-Specific Shared Contract

Function signature:

```python
async def summarize_slice(
    slice_messages: list[BaseMessage],
    summarizer_model_id: str,
    task_id: str,
    tenant_id: str,
    agent_id: str,
    checkpoint_id: str | None,
    cost_ledger: CostLedgerRepository,
    callbacks: list[BaseCallbackHandler] | None = None,
) -> SummarizeResult
```

`SummarizeResult`:

```python
@dataclass(frozen=True)
class SummarizeResult:
    summary_text: str | None        # None iff skipped
    skipped: bool                   # True iff summarizer failed after retries
    summarizer_model_id: str        # echoed back for event emission
    tokens_in: int                  # prompt token count
    tokens_out: int                 # completion token count
    cost_microdollars: int          # rolled-up cost
    latency_ms: int
```

Semantics:

- If `slice_messages` has < 2 entries, returns `SummarizeResult(summary_text=None, skipped=True, ...)` (not enough to summarize).
- Builds the summarizer prompt per the design doc's §Tier 3 spec:
  - `SystemMessage` with the fixed `SUMMARIZER_PROMPT` string (see below).
  - `HumanMessage` with a formatted serialisation of `slice_messages` (see below).
- Calls the summarizer model via `langchain.chat_models.init_chat_model` (or the worker's existing model-init helper — reuse Track 5's pattern).
- Retries up to `SUMMARIZER_MAX_RETRIES` on transient errors (use the worker's existing `_is_retryable_error` classification from `graph.py`); on exhaustion, emits `compaction.tier3_skipped` log and returns `skipped=True`.
- On success: writes one row to `agent_cost_ledger` with `operation='compaction.tier3'`, `model_id=summarizer_model_id`, tokens/cost from the response metadata. Uses the repository pattern.
- Langfuse: if a Langfuse callback is passed in, the LLM call is automatically traced; no extra work beyond passing the callbacks list through to `ainvoke`.
- Cost lookup: use `_get_model_cost_rates(summarizer_model_id)` (already in `graph.py`) or an equivalent lookup helper. If the model has zero cost rates (dev-mode), still write the ledger row with `cost_microdollars=0`.
- Deterministic replay is best-effort. The caller is responsible for checkpointing the returned `summary_text`; crash mid-summarizer produces a retry that may return different text — accepted per design.

### `SUMMARIZER_PROMPT`

Defined as a module-level string constant:

```
You are compressing a portion of an autonomous agent's tool-use history so the
agent can continue the task within its context window. Produce a compact
factual summary (at most 400 words) that preserves:
- Files the agent has created, read, or modified (full paths)
- External URLs or API responses whose contents matter for the rest of the task
- Decisions the agent has committed to and their reasoning
- Errors encountered and whether they were resolved
- Parameters or identifiers the agent will need later (IDs, keys, names)

Do NOT:
- Address the agent in the second person.
- Invent next steps or give instructions.
- Comment on the compression itself.
Return the summary only.
```

### Slice serialisation helper

`format_messages_for_summary(slice_messages: list[BaseMessage]) -> str` — produces a deterministic textual representation:

- `SystemMessage` → `"SYSTEM: <content>"`
- `HumanMessage` → `"USER: <content>"`
- `AIMessage` → `"ASSISTANT (step N): <content>\n  tool_calls: [<name>(...), ...]"` where the args dict is rendered via `json.dumps(sort_keys=True)`.
- `ToolMessage` → `"TOOL_RESULT (call_id=..., name=...): <content>"`.
- Indexes are the positions in the input `slice_messages`, 0-based, so the summary can reference them.

Determinism is load-bearing — sort keys in JSON dumps, use a fixed step-index naming scheme. If two callers with the same slice produce different serialisations, the KV-cache on the summary invocation itself drops.

## Affected Component

- **Service/Module:** Worker Service — Compaction
- **File paths:**
  - `services/worker-service/executor/compaction/summarizer.py` (new)
  - `services/worker-service/executor/compaction/__init__.py` (modify — re-export `summarize_slice`, `SummarizeResult`)
  - `services/worker-service/tests/test_compaction_summarizer.py` (new)
- **Change type:** new module + unit tests (with mocked LLM client)

## Dependencies

- **Must complete first:** Task 2 (`SUMMARIZER_MAX_RETRIES`, `PLATFORM_DEFAULT_SUMMARIZER_MODEL`, `get_platform_default_summarizer_model`).
- **Provides output to:** Task 7 (pipeline orchestrator calls `summarize_slice` as the Tier 3 step, branches on `skipped`).
- **Shared interfaces/contracts:** The `SummarizeResult` type + `summarize_slice` function.

## Implementation Specification

Follow Track 5's `_build_summarizer_callable` shape closely — it already handles the cheap-model-via-init_chat_model pattern, extracts token metadata, and understands the worker's retry landscape. The main differences:

- Cost ledger tag is `'compaction.tier3'` instead of `'memory.write'`.
- Prompt is Track 7's `SUMMARIZER_PROMPT`, not Track 5's memory-entry prompt.
- Output is a single string (the summary), not a structured title+summary+tags tuple.

Pseudocode for the retry loop:

```python
async def summarize_slice(...):
    if len(slice_messages) < 2:
        return SummarizeResult(summary_text=None, skipped=True, summarizer_model_id=summarizer_model_id, tokens_in=0, tokens_out=0, cost_microdollars=0, latency_ms=0)

    serialized = format_messages_for_summary(slice_messages)
    prompt = [
        SystemMessage(content=SUMMARIZER_PROMPT),
        HumanMessage(content=serialized),
    ]
    llm = init_chat_model(summarizer_model_id, ...)

    last_error = None
    for attempt in range(SUMMARIZER_MAX_RETRIES + 1):
        try:
            started = time.monotonic()
            response = await llm.ainvoke(prompt, config={"callbacks": callbacks or []})
            latency_ms = int((time.monotonic() - started) * 1000)
            tokens_in, tokens_out = _extract_tokens(response.response_metadata or {})
            cost_microdollars = _rollup_cost(
                tokens_in, tokens_out, summarizer_model_id
            )
            _write_cost_ledger_row(
                cost_ledger, tenant_id, agent_id, task_id, checkpoint_id,
                summarizer_model_id, tokens_in, tokens_out,
                cost_microdollars, operation='compaction.tier3',
            )
            return SummarizeResult(
                summary_text=response.content,
                skipped=False,
                summarizer_model_id=summarizer_model_id,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                cost_microdollars=cost_microdollars,
                latency_ms=latency_ms,
            )
        except Exception as e:
            last_error = e
            if not _is_retryable_error(e):
                break
            backoff = _get_retry_after(e) or min(30, 2 ** attempt)
            await asyncio.sleep(backoff)

    log_structured(
        "compaction.tier3_skipped",
        tenant_id=tenant_id,
        agent_id=agent_id,
        task_id=task_id,
        summarizer_model=summarizer_model_id,
        retries_exhausted=SUMMARIZER_MAX_RETRIES,
        last_error=str(last_error)[:200] if last_error else None,
    )
    return SummarizeResult(
        summary_text=None,
        skipped=True,
        summarizer_model_id=summarizer_model_id,
        tokens_in=0, tokens_out=0, cost_microdollars=0, latency_ms=0,
    )
```

Reuse `_extract_tokens`, `_is_retryable_error`, `_get_retry_after` from `graph.py` (or move them into a shared `worker_util.py` in this task if they're private helpers on `GraphExecutor`).

## Acceptance Criteria

- [ ] `summarize_slice` returns a non-None `summary_text` on a happy-path mocked LLM call and writes one `agent_cost_ledger` row with `operation='compaction.tier3'`.
- [ ] Slice of < 2 messages returns `skipped=True` with no LLM call and no ledger write.
- [ ] On a retryable error followed by success, the function succeeds and records one ledger row (not three).
- [ ] On exhaustion of retries, the function returns `skipped=True`, emits `compaction.tier3_skipped` log, and writes NO ledger row.
- [ ] `format_messages_for_summary` is deterministic: two calls on the same slice produce byte-identical output (assert via unit test).
- [ ] Cost ledger row fields match the existing schema — `tenant_id`, `agent_id`, `task_id`, `checkpoint_id`, `model_id`, `tokens_in`, `tokens_out`, `cost_microdollars`, `operation`.
- [ ] Langfuse callbacks passed in propagate into the `ainvoke` call.
- [ ] `make worker-test` — unit tests pass with a mocked LangChain `init_chat_model`.

## Testing Requirements

- **Unit tests:** mock `init_chat_model` via `unittest.mock.patch` or equivalent; cover happy path, empty-slice, retry-then-success, retry-exhausted, cost-ledger row shape, callback propagation.
- **Determinism test:** call `format_messages_for_summary` twice on the same slice; assert byte-equal.
- **No live LLM call in CI.** The tests must run offline without any provider credentials.

## Constraints and Guardrails

- Do not call the summarizer from this module at import time. No top-level network.
- Do not hard-code the summarizer model — always read it from the caller's argument. The caller (Task 7 pipeline) resolves the effective model from agent config + platform default.
- Do not use `budget_max_per_task` gating here — the Track 3 per-step budget carve-out that skips Tier 3's pause check lives in Task 7 (at the pipeline or worker post-astream layer).
- Do not silently swallow non-retryable errors — raise them, and let the caller surface the failure through the existing error path.
- Do not write the summary to any state field — that is Task 7's job (pipeline merges the result into `summary_marker`).

## Assumptions

- The worker already has `init_chat_model` resolution and provider-credential plumbing from Phase 1 + Track 1.
- Token extraction helpers (`_extract_tokens`) exist in `graph.py` from Track 5 — reuse them rather than re-implementing.
- Retryable-error classification (`_is_retryable_error`) likewise exists.

<!-- AGENT_TASK_END: task-6-tier-3-summarizer.md -->
