<!-- AGENT_TASK_START: task-2-recursive-chunking.md -->

# Task 2 — Recursive Chunking for Oversized Summarizer Middles

## Agent Instructions

**CRITICAL PRE-WORK:**
1. Read `docs/design-docs/phase-2/track-7-context-window-management.md` — §"Tier 3: retrospective summarization" and the Option-3 (replace-and-rehydrate) pivot notes. Under Option 3 the region to summarise is the **middle** — the messages between `summarized_through` and `keep_window_start`.
2. Read `services/worker-service/executor/compaction/summarizer.py` — `summarize_slice` signature, retry loop, `SummarizeResult` dataclass, deterministic prompt serialization in `format_messages_for_summary`.
3. Read `services/worker-service/executor/compaction/tokens.py` — token estimator used elsewhere in the pipeline.
4. GH issue [#82](https://github.com/shenjianan/persistent-agent-runtime/issues/82) — this task is phase-1c ("recursive chunking").

**CRITICAL POST-WORK:**
1. Run `make worker-test`. All existing tests green; new chunking tests green.
2. Update `progress.md` to mark Task 2 Done.

---

## Context

Today the summarizer is called with one middle as a single `llm.ainvoke`. On agents with very large context windows (Claude 4.6/4.7 at 1M tokens), the middle to summarise can exceed the summarizer model's own context window — e.g. middle = 700K tokens, summarizer = Claude Haiku 4.5 with a 200K window. Today that would fail the `ainvoke` call with a provider context-length error, summarization would be classified as a fatal provider error, and the task would dead-letter.

Task 3 (the `pre_model_hook` + replace-and-rehydrate rewrite) makes this scenario more likely — raw middles carry full tool-result payloads, not Tier-1-stubbed views. Chunking must land first so Task 3 is safe.

## Goal

Handle middles that exceed the summarizer's effective context budget by recursively chunk-summarising. A middle of any size produces a bounded summary output without exceeding the summarizer model's input limit.

## Contract — Behaviour Changes

### 1. `_chunk_summarize` helper

New private function in `summarizer.py` (preferred) or a sibling module. Signature sketch:

```
async def _chunk_summarize(
    middle_messages: list[BaseMessage],
    prior_summary: str,
    summarizer_model_id: str,
    summarizer_context_window: int,
    ...task/tenant/agent context + cost_ledger + callbacks...,
) -> SummarizeResult
```

Behavioural contract (implementer chooses code shape):

- **Gate input on the actual summarizer payload, not raw messages.** The summarizer sends `[SystemMessage(SUMMARIZER_PROMPT), HumanMessage(prior_summary + format_messages_for_summary(middle))]` plus reserved `max_tokens` for output. Estimate the full payload: `tokens(SUMMARIZER_PROMPT) + tokens(prior_summary) + tokens(format_messages_for_summary(middle)) + SUMMARIZER_MAX_OUTPUT_TOKENS (from Task 1) + SUMMARIZER_INPUT_HEADROOM_TOKENS`. If this fits in `summarizer_context_window`, delegate to the existing single-call path unchanged. Otherwise recurse.
- **`prior_summary` carry-through contract.** `prior_summary` is passed ONLY to the TOP-LEVEL `summarize_slice` call (outside recursion). Recursive per-chunk calls receive `prior_summary=""` so every recursion level doesn't bloat with a duplicated summary. The final concatenation call (which runs on the synthetic slice of per-chunk summary `AIMessage`s) is the one that re-introduces `prior_summary`, producing a single coherent summary over `prior_summary + concatenated chunk summaries`.
- **Progress guarantee.** When recursion is required, the chosen split index MUST satisfy `0 < split < len(middle)` — a boundary at index 0 or at `len(middle)` means no progress and must be rejected.
- **Safe boundary** (preferred): the split index lands between a user-turn or text-only-AIMessage boundary, not between an AIMessage-with-tool_calls and any of its ToolMessages. Reuse / extract the alignment logic already in `compact_for_llm` (walk back to the preceding AIMessage with `tool_calls` if the midpoint lands on a ToolMessage).
- **Fallback when no safe interior boundary exists** (e.g. the middle is one giant AIMessage with a long trailing run of ToolMessages — the only safe split IS index 0): fall back to **unsafe halving** at `len(middle) // 2` with a logged `compaction.tier3_unsafe_chunk_split` WARN. The downstream single-call summarizer receives the unsafe halves; tool-pair context across the split boundary is represented via the summary text rather than structural preservation. This is acceptable because the summarizer never sees tool-pair-enforcing providers — it consumes the middle as serialised text via `format_messages_for_summary`. The WARN is the signal that the middle shape was pathological.
- Intermediate results: each recursive call returns a `SummarizeResult`. Build a synthetic middle of `AIMessage(content=<child summary text>)` messages and call the single-call summarizer on that synthetic middle (with the original `prior_summary`) to produce the final summary.

### 2. Cost ledger accounting

- Every LLM call (each chunk summary + the final concatenation summary) writes one row to the cost ledger via the existing `cost_ledger.insert` interface, tagged `operation="compaction.tier3.chunk"` for intermediate calls and `operation="compaction.tier3"` for the final one. Intermediate rows use the same `checkpoint_id` / `summarized_through_turn_index_after` so idempotency keys stay well-formed.
- The `SummarizeResult` returned to the pipeline accumulates `tokens_in`, `tokens_out`, `cost_microdollars` across all sub-calls.

### 3. Failure handling

- Retry semantics from `summarize_slice`'s existing retry loop apply per-call (each chunk is independent).
- If any chunk fails after retries, the top-level `SummarizeResult` is `skipped=True, skipped_reason="retryable"` (or `"fatal"` if the underlying failure was fatal). Pipeline's existing watermark-don't-advance behaviour applies.
- **No partial summary** — if chunking can't complete, no summary is written. Strict-append invariant preserved.

## Affected Files

- `services/worker-service/executor/compaction/summarizer.py` — `_chunk_summarize` helper, `SUMMARIZER_INPUT_HEADROOM_TOKENS` constant, single-vs-chunk dispatch at the top of `summarize_slice`.
- `services/worker-service/executor/compaction/defaults.py` — `SUMMARIZER_INPUT_HEADROOM_TOKENS = 12_000`. Budget: accounts for the ~1K-token `SUMMARIZER_PROMPT`, `SUMMARIZER_MAX_OUTPUT_TOKENS` reservation (1500 from Task 1), serialisation overhead (`format_messages_for_summary` adds ~10% over raw token count for JSON-dumps of tool args), plus per-provider safety margin. On a 200K summarizer, this leaves ~188K for `prior_summary + serialised middle` — still massive. If Task 1's output cap rises per its Task-3 interaction clause, bump headroom proportionally.
- `services/worker-service/executor/compaction/pipeline.py` — no behaviour change needed if dispatch happens inside `summarize_slice`. If implementer decides to dispatch in the pipeline, the `compact_for_llm` tier-3 branch must pass `summarizer_context_window` (already resolvable from `models.context_window` lookup for the summarizer model — extend signature only if necessary).
- `services/worker-service/tests/test_compaction_chunking.py` — new file with tests per §Acceptance Criteria.

## Dependencies

None at code level. Schedule-wise: Task 2 must land before Task 3. Canonical landing order for this track is **2 → 4 → 3 → 5**.

## Out of Scope for This Task

- Parallelising chunk summarisation (could cut latency ~2×; defer until real cost/latency telemetry demands it).
- More elaborate splitting heuristics (e.g. topic-aware). Halves-with-safe-boundary is sufficient for a sound baseline.
- Changing when summarization fires — that stays owned by the `pre_model_hook` trigger policy (Task 3).

## Acceptance Criteria (observable behaviours)

1. Middle of 200 small messages, estimated 3K tokens, 200K summarizer — `_chunk_summarize` produces ONE LLM call (fast path, no recursion). Single cost-ledger row. Matches pre-Task-2 behaviour byte-for-byte.
2. Middle of 1000 large messages whose serialised `format_messages_for_summary` output is estimated at 400K tokens against a 200K summarizer — `_chunk_summarize` produces THREE LLM calls (two halves + one concatenation). Three cost-ledger rows. Output is a single string summary returned as `SummarizeResult.summary_text`.
3. **Gate correctness:** the fast-path vs recurse decision uses the full-payload estimate (prompt + prior_summary + serialised middle + max_tokens reservation), NOT the raw `middle_messages` token count. Explicit test: middle with 190K raw tokens but whose serialised form is 210K tokens against a 200K window triggers recursion, not the fast path.
4. **`prior_summary` carry-through:** test that the top-level call receives a non-empty `prior_summary`, the two recursive per-chunk calls receive `prior_summary=""`, and the final concatenation call receives the original `prior_summary`. Assert via captured LLM-call arguments.
5. Chunk split never separates an AIMessage with `tool_calls` from its corresponding `ToolMessage`(s) **when an interior safe boundary exists**. Verified by a test that constructs a middle whose natural midpoint lands on a ToolMessage and asserts the actual split index walked back to a preceding AIMessage with tool_calls.
6. **Progress guarantee:** split index is always strictly `0 < split < len(middle)`. Test with a one-giant-AIMessage + long-trailing-ToolMessages middle (no interior safe boundary): the fallback path fires, `compaction.tier3_unsafe_chunk_split` WARN is emitted exactly once, chunking completes in bounded recursion depth (`O(log len)`).
7. If a chunk summary call fails with a retryable error, the top-level result is `skipped=True, skipped_reason="retryable"`. No summary state advancement. Partial-results are NOT persisted.
8. If a chunk summary call fails with a fatal error, the top-level result is `skipped=True, skipped_reason="fatal"` and `tier3_fatal_short_circuited=True` is set in state (matches pre-Task-2 fatal-short-circuit semantics).
9. Regression test: existing `test_compaction_summarizer.py` passes unchanged.

## Pattern references in existing code

- Boundary-walk logic (AIMessage alignment): `services/worker-service/executor/compaction/pipeline.py` tier-3 branch (`protect_from_index` walkback introduced in PR #80's hardening commit). Consider extracting to a shared helper.
- Deterministic serialisation (cache-stability): `format_messages_for_summary` in `summarizer.py` — must continue to produce byte-identical output for the same slice across repeated firings.
- Cost-ledger idempotency: `services/worker-service/core/cost_ledger_repository.py` — `ON CONFLICT DO NOTHING` on `(tenant_id, task_id, checkpoint_id, operation, summarized_through_turn_index_after)`.

<!-- AGENT_TASK_END -->
