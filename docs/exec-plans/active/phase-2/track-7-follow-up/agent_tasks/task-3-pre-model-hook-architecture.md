<!-- AGENT_TASK_START: task-3-pre-model-hook-architecture.md -->

# Task 3 — Replace Compaction Pipeline with `pre_model_hook` + Replace-and-Rehydrate Architecture

## Agent Instructions

**CRITICAL PRE-WORK:**
1. Read `docs/design-docs/phase-2/track-7-context-window-management.md` end-to-end — especially "Core design rules" and the Tier 1 / 1.5 / 3 definitions you are about to delete. Understand what invariants the new architecture preserves vs which assumptions it overturns.
2. Read `services/worker-service/executor/compaction/pipeline.py` in full. Trace `compact_for_llm` end-to-end: Tier 1 stubbing, Tier 1.5 arg truncation, Tier 3 summarize-and-rebuild, watermark updates. This file is getting a major rewrite.
3. Read `services/worker-service/executor/compaction/state.py` — the `RuntimeState` TypedDict, its reducers (strict-append `summary_marker_reducer`, monotone `summarized_through` reducer), and counters.
4. Read `services/worker-service/executor/compaction/transforms.py` — `clear_tool_results` (Tier 1) and `truncate_tool_call_args` (Tier 1.5). Both are being deleted.
5. Read `services/worker-service/executor/graph.py` — how `agent_node` currently invokes `compact_for_llm` and how `_build_runnable_config` assembles runtime config. You'll wire `pre_model_hook` in here.
6. Read LangGraph's `pre_model_hook` documentation and the `create_react_agent` integration. The hook returns state updates plus `llm_input_messages` for a non-persistent projection.
7. Read DeepAgents' `SummarizationMiddleware` (LangChain middleware package) — the canonical pre-model summarization pattern we're adopting.
8. Read Track 7 Task 9 (`pre_tier3_memory_flush`) — the one-shot memory-flush behaviour must survive the rewrite.
9. Read Track 7 Task 10 (`context_exceeded_irrecoverable` dead-letter path) — still the escape valve when chunked summarization cannot get below the window.
10. **Task 2 (recursive chunking) and Task 4 (S3 offload at ingestion) MUST both be merged first.** Task 2 because raw middles on 1M-context agents will overflow the summarizer. Task 4 because the new architecture assumes `state.messages` never holds raw blobs over `OFFLOAD_THRESHOLD_BYTES` — without Task 4, every projection on every turn would be bloated.

**CRITICAL POST-WORK:**
1. Run `services/worker-service/.venv/bin/python -m pytest services/worker-service/tests/ -x`. All tests green.
2. Run `make worker-test` once for full coverage.
3. Update `progress.md` to mark Task 3 Done.

---

## Context

Track 7 shipped a three-tier compaction pipeline that mutates `state.messages` between LLM turns. Tier 1 rewrites old `ToolMessage.content` to `"[tool output not retained …]"` stubs; Tier 1.5 truncates large `AIMessage.tool_calls` arg payloads; Tier 3 appends a summary via the `summary_marker` strict-append reducer and rebuilds a tail slice. The pipeline works, but has three structural costs:

1. **Summarizer sees stubs.** Tier 3 reads the post-Tier-1 view, so summaries describe placeholders instead of real tool output. This is the quality gap Task 3 (prior spec) tried to patch by teaching Tier 3 to read raw. Option A was a bandage on an architecture that mutates the source of truth.
2. **`state.messages` is not append-only.** Every tier mutates it. Debuggability, replay, and KV-cache reasoning all pay for this.
3. **The "two views" live only inside one function call.** Any caller outside `compact_for_llm` (inspection, conversation-log rendering, future middleware) has to recompute from raw.

The canonical pattern in LangGraph — used by DeepAgents' `SummarizationMiddleware` and now broadly recommended upstream — is a `pre_model_hook` that fires before every agent LLM call and returns a non-persistent message projection via `{"llm_input_messages": projection}`. State stays append-only; the projection is recomputed each turn from `(summary, raw messages, keep window)`. We are replacing Track 7's pipeline with this pattern.

## Goal

Delete `compact_for_llm` and the Tier 1 / 1.5 transforms. Replace them with a `pre_model_hook` wired into `create_react_agent` (via `agent_node`). The hook computes a three-region projection — `summary` + `middle` + `keep window` — returned as `llm_input_messages` without persisting the shape. Summarization fires inside the hook when the projection's estimated token count crosses `COMPACTION_TRIGGER_FRACTION = 0.85` of the agent's `model_context_window`; the summarizer receives raw (never-stubbed) content. The only write to `state.messages` remains Task 5's Option C recalled-reference replacement. All other state mutations are confined to the new `summary` field (replace semantics) and the existing `summarized_through_turn_index` monotone counter.

## Contract — Behaviour Changes

### 1. `pre_model_hook` is the new compaction entry point

Introduce a function in `compaction/pipeline.py` whose shape is compatible with LangGraph's `pre_model_hook` protocol. It is invoked by `create_react_agent` (configured in `agent_node`) before each agent LLM call. It returns a dict containing:

- `llm_input_messages` — the three-region projection (see §2). Non-persistent; LangGraph does NOT write this back to `state.messages`.
- Zero or more state updates when summarization fires: `summary`, `summarized_through_turn_index`, counter updates (`tier3_firings_count`, `tier3_fatal_short_circuited`, `memory_flush_fired_this_task`, `last_super_step_message_count`).

The hook is `async` (may call the summarizer LLM). It must be deterministic for a given `(state, config)` input modulo the summarizer LLM call itself.

### 2. Three-region projection

For each call, compute:

- **Summary region** — a single `SystemMessage` rendered from `state.summary` (string, possibly empty), representing everything at indices `[0 : summarized_through_turn_index]`. Omitted when `summary` is empty.
- **Middle region** — `state.messages[summarized_through_turn_index : keep_window_start]`. Verbatim, never stubbed, never argument-truncated.
- **Keep window** — `state.messages[keep_window_start : ]`. Positional slice: walk back from the end past the 3rd-most-recent `ToolMessage` (`KEEP_TOOL_USES = 3`), then align `keep_window_start` to the preceding `AIMessage` with `tool_calls` (orphan-prevention pattern shipped in PR #80 — reuse / extract that helper). `HumanMessage`s and text-only `AIMessage`s inside the window are included verbatim.

The system prompt (from agent config) is prepended as the first message. Final projection order: `[SystemMessage(system_prompt), SystemMessage(summary)?, *middle, *keep_window]`.

**Illustrative pseudocode (shape only — NOT a copy-paste blueprint):**

```python
# illustrative — implementer uses actual LangGraph hook conventions
async def compaction_pre_model_hook(state, config):
    raw = state["messages"]
    summary = state.get("summary", "")
    summarized_through = state.get("summarized_through_turn_index", 0)

    keep_window_start = find_keep_window_start(raw)  # reuses PR #80 helper
    middle = raw[summarized_through:keep_window_start]
    keep_window = raw[keep_window_start:]

    estimated = estimate_projection_tokens(summary, middle, keep_window)
    state_updates = {}

    if estimated > COMPACTION_TRIGGER_FRACTION * model_context_window:
        # optional pre-Tier-3 memory flush (Track 7 Task 9) still fires here
        new_summary = await summarize(
            prior_summary=summary,
            middle_messages=middle,   # raw — no Tier 1 stubbing
            ...,
        )
        state_updates["summary"] = new_summary
        state_updates["summarized_through_turn_index"] = keep_window_start
        state_updates["tier3_firings_count"] = state["tier3_firings_count"] + 1
        projection_body = [SystemMessage(new_summary), *keep_window]  # middle empty post-compaction
    else:
        projection_body = ([SystemMessage(summary)] if summary else []) + middle + keep_window

    projection = [SystemMessage(system_prompt), *projection_body]
    return {"llm_input_messages": projection, **state_updates}
```

### 3. State schema changes

- **Remove** from `RuntimeState`: `summary_marker`, `cleared_through_turn_index`, `truncated_args_through_turn_index`. Delete the strict-append `summary_marker_reducer` and the two monotone counters' reducers (only if no longer referenced elsewhere — verify via grep).
- **Keep**: `summarized_through_turn_index` (monotone reducer unchanged), `tier3_firings_count`, `tier3_fatal_short_circuited`, `memory_flush_fired_this_task`, `last_super_step_message_count`.
- **Add**: `summary: str` with a simple replace reducer (new value wins). Default is empty string on fresh tasks. No seeding from legacy `summary_marker` — accept the one-time cost on in-flight tasks (see §Rollout).

### 4. Summarizer integration

- Summarizer is invoked only from inside the hook when the threshold is crossed.
- Input = `prior_summary + format_messages_for_summary(middle)` — raw middle, no stubs, no arg truncation.
- Uses Task 2's recursive chunking when the serialized input exceeds the summarizer's context window.
- Output bounded by Task 1's `SUMMARIZER_MAX_OUTPUT_TOKENS = 1500`.
- On success: `state.summary` is REPLACED (not appended) with the new summary text. `state.summarized_through_turn_index` advances to `keep_window_start`. The projection rebuilt for this same LLM call uses the new summary and an empty middle.
- On retryable failure: no state changes. Middle stays; next turn retries the threshold check.
- On fatal failure: set `tier3_fatal_short_circuited=True`. Downstream Track 7 Task 10 logic handles dead-letter if the raw middle is so large even chunked summarization can't bring the projection below `model_context_window`.

### 5. Memory flush preserved

The pre-Tier-3 memory flush from Track 7 Task 9 still fires inside the hook, with identical semantics: one-shot per task, requires `agent.memory.enabled` AND `agent.context_management.pre_tier3_memory_flush` both true, skipped on heartbeat turns. Only the trigger site moves — from inside `compact_for_llm`'s Tier 3 branch into the pre-model hook's summarize path. `memory_flush_fired_this_task` idempotency guard unchanged.

### 6. `state.messages` is (near-)append-only

The only write to `state.messages` permitted by this architecture is Task 5's Option C recalled-reference replacement (when a previously recalled `ToolMessage` ages past the summarization watermark, its content is replaced with a reference marker). Every other flow must leave `state.messages` untouched; compaction effects live in `summary` + `summarized_through` + projection.

### 7. Tier 1 and Tier 1.5 are gone

`clear_tool_results` and `truncate_tool_call_args` are deleted. Their responsibilities are absorbed by:

- **Task 4 (S3 offload at ingestion)** — large tool results and large `tool_calls` args are replaced with preview + reference at the moment they enter `state.messages`. The projection can therefore feed raw middles to both the main LLM and the summarizer without risk of multi-MB content bloating the payload.
- **The projection itself** — old content that would previously be Tier-1-stubbed now simply lives behind the summarization watermark once a compaction fires, and the summary string is its compact representation.

No "placeholder" strings ever reach the main LLM under the new architecture.

## Affected Files

- `services/worker-service/executor/compaction/pipeline.py` — major rewrite. Delete `compact_for_llm`. Add the `pre_model_hook`-shaped entry point, keep window finder (extracted from the existing PR #80 walkback), projection builder, and in-hook summarization branch.
- `services/worker-service/executor/compaction/state.py` — update `RuntimeState` TypedDict fields per §3. Delete `summary_marker_reducer`. Add replace reducer for `summary` (or rely on TypedDict default-overwrite if no reducer needed).
- `services/worker-service/executor/compaction/transforms.py` — delete `clear_tool_results` and `truncate_tool_call_args`. If the file has no remaining helpers, delete it (and update imports). Otherwise, keep only what survives.
- `services/worker-service/executor/compaction/summarizer.py` — signature of summarize entry point may change (takes `prior_summary: str + middle: list[BaseMessage]` instead of full slice). `format_messages_for_summary` behaviour unchanged.
- `services/worker-service/executor/graph.py` — `agent_node` wires `pre_model_hook=compaction_pre_model_hook` into `create_react_agent` (or the equivalent agent construction path). `_build_runnable_config` passes through any context the hook needs (tenant/agent/task/cost-ledger/callbacks).
- `services/worker-service/tests/test_compaction_pipeline.py` — delete or rewrite; the Tier 1 / 1.5 cases are obsolete. New tests for hook behaviour.
- `services/worker-service/tests/test_compaction_transforms_*.py` — delete. Tier 1 / 1.5 don't exist.
- `services/worker-service/tests/test_compaction_shape_property.py` — update Hypothesis property test to target the new projection. The invariants (shape validity, tool-pair pairing, no orphan ToolMessages) must still hold.
- `services/worker-service/tests/shape_validator.py` — no changes expected; the validator runs against the new `llm_input_messages` projection unchanged.
- `services/worker-service/tests/test_pre_model_hook.py` — new file covering the acceptance criteria below.

## Dependencies

- **Task 2 (recursive chunking)** MUST land first. Raw middles on 1M-context agents can exceed a 200K-summarizer window; without chunking, the first post-deploy summarization on a long-running task would dead-letter.
- **Task 4 (S3 offload at ingestion)** MUST land first. The new architecture assumes `state.messages` only holds lean entries. Without Task 4, huge tool results would stay inline and bloat every projection.
- Canonical track-wide order: **2 → 4 → 3 → 5**. Tasks 1, 6, 7 are independent.

## Out of Scope for This Task

- Task 5's Option C recalled-reference replacement (the one exception to append-only). Task 3 only leaves room for it; implementation is Task 5.
- Parallelising the hook with other pre-LLM work. Sequential is fine.
- Persisting the projection. By design it's ephemeral per-turn.
- Changing the agent-loop topology. The graph still has a single agent node driving tool calls; only the pre-LLM step changes.
- Seeding `summary` from legacy `summary_marker` on first post-deploy turn. Accepted one-time cost — see §Rollout.

## Rollout

- **No DB migration.** State schema change is TypedDict-level; missing fields on load are tolerated by the TypedDict, and removed-field presence on older checkpoints is harmless (ignored at read time).
- **In-flight tasks.** On the first post-deploy turn for any task with a non-empty legacy `summary_marker`, the new `summary` field starts empty. The first compaction will summarize the entire pre-existing middle fresh. On large middles this is expensive — Task 2's chunking absorbs it. One-time cost, acceptable.
- **KV-cache invalidation.** The projection shape changes (system prompt slot, single summary `SystemMessage` instead of appended marker), so cache prefixes from pre-deploy turns will miss on the first post-deploy turn. Re-warms normally from turn two. Documented as acceptable.
- **Rollback path.** Revert the deploy. Old reducers and old fields can coexist with new ones in `RuntimeState` during a short blast-radius window if the rewrite is done carefully — prefer keeping legacy field definitions around (unused by the new code) for one release cycle to simplify rollback. Clean them up in a follow-up commit after soak.
- **Structural equivalence (NOT content equivalence).** For any given `(state.messages, agent config)`, the new `llm_input_messages` preserves the same structural invariants Track 7 guaranteed: same last-N-tool-uses window, same tool-use/tool-result pairing, same `SystemMessage`-first ordering, same shape-validator outcomes. The *contents* differ meaningfully from Track 7: old tool results are represented by the summary string instead of `"[tool output not retained …]"` stubs, and the summary is replaced rather than appended. Shape validators and the Hypothesis property test continue to pass; any test that asserted on the specific stub string is obsolete (Acceptance #5).

## Acceptance Criteria (observable behaviours)

1. `compact_for_llm` no longer exists. `agent_node` wires `pre_model_hook` into the agent construction path; no call site of `compact_for_llm` remains in the codebase (verify via grep).
2. `RuntimeState` contains `summary: str`, `summarized_through_turn_index`, `tier3_firings_count`, `tier3_fatal_short_circuited`, `memory_flush_fired_this_task`, `last_super_step_message_count`. It does NOT contain `summary_marker`, `cleared_through_turn_index`, `truncated_args_through_turn_index`.
3. On every agent LLM turn, `llm_input_messages` returned by the hook is a list whose order matches `[SystemMessage(system_prompt), SystemMessage(summary)?, *middle, *keep_window]`. `tests/shape_validator.py` passes.
4. **Raw content to summarizer:** when summarization fires, the mock summarizer receives middle messages whose `ToolMessage.content` is the full original content, NOT `"[tool output not retained …]"` or any placeholder. Verified via a test where the agent has 10 historical 8KB tool results and a compaction trigger.
5. **Main LLM sees no stubs.** In the same test, the main LLM's received `messages` (captured via mock on `llm_with_tools.ainvoke`) either contains the raw content (if still in middle/keep window) OR the post-summarization projection (summary + keep window) — never stubs.
6. **Post-summarization state:** `state.summary` equals the new summary string (replace, not append). `state.summarized_through_turn_index` equals the `keep_window_start` used in the firing turn. `state.messages` is byte-identical before and after the firing.
7. **Threshold:** summarization fires iff `estimate_projection_tokens >= COMPACTION_TRIGGER_FRACTION * model_context_window` (0.85). Below the threshold, no summarizer call is made, no state updates beyond the turn's normal append.
8. **Keep window correctness:** `keep_window_start` always points to an `AIMessage` with `tool_calls` (or the start of the list when there are fewer than `KEEP_TOOL_USES` tool uses in history). No orphan `ToolMessage` ever appears as the first element of the keep window. Existing PR #80 regression cases (`test_compaction_tier3_tool_boundary.py`, `test_compaction_tier3_second_firing_boundary.py` — or their rewrites) pass.
9. **Pre-Tier-3 memory flush (Track 7 Task 9) preserved.** Memory-enabled agent with `pre_tier3_memory_flush=true`, non-heartbeat turn, first firing in task: flush hook is invoked before the summarizer. Second firing in the same task: flush hook is NOT invoked (one-shot guard via `memory_flush_fired_this_task`).
10. **Hypothesis property test.** `tests/test_compaction_shape_property.py` (updated to target the new projection) passes. Invariants: no orphan `ToolMessage`, each `AIMessage`'s `tool_calls` are immediately followed by matching `ToolMessage`s in `llm_input_messages`, `SystemMessage`s only at the head.
11. **Chunking integration (Task 2).** With a raw middle large enough that `format_messages_for_summary(middle)` exceeds the summarizer's context window, the summarization call succeeds via Task 2's recursive chunking — no provider context-length error, one final `SummarizeResult` returned to the hook.
12. **Dead-letter path (Track 7 Task 10).** If, even after chunked summarization, `estimate_projection_tokens > model_context_window` (e.g. pathological single-turn with a 500K tool result somehow inline), the task dead-letters with `failure_reason_code=context_exceeded_irrecoverable`. Regression test preserved.
13. **Firing-rate regression.** On the Task 6 offline eval suite's representative long-tool-use scenario (or the production-reproducing `1717c12b-aee3-4632-b29e-b3d0e269e87f` replay if available), `tier3_firings_count / llm_call_count <= 1/50`. Rich summaries + unchanged trigger threshold must not regress firing rate.
14. **Append-only invariant.** Test that runs a 30-turn agent session and asserts `state.messages` is strictly a prefix of `state.messages` at every later super-step, except for Task 5's recalled-reference replacement (which Task 3 does NOT implement — the assertion is unconditional on Task 3's code path).

## Pattern references in existing code

- **LangGraph `pre_model_hook`** — upstream docs on `create_react_agent`'s `pre_model_hook` argument and the `llm_input_messages` convention for non-persistent projections.
- **DeepAgents `SummarizationMiddleware`** — the canonical pattern we're adopting. Good reference for summary-plus-keep-window composition and the replace-semantics reducer for the summary field.
- **Keep-window boundary walk** — `services/worker-service/executor/compaction/pipeline.py` Tier 3 branch post-PR #80 (`protect_from_index` walkback). Extract into a shared helper and reuse from the new hook.
- **Shape validator** — `services/worker-service/tests/shape_validator.py`. Unchanged; runs against the new projection.
- **Hypothesis property test** — `services/worker-service/tests/test_compaction_shape_property.py`. Update test wiring to call the new hook; invariants unchanged.
- **Deterministic serialisation** — `format_messages_for_summary` in `summarizer.py`. Must keep producing byte-identical output for repeated firings on the same middle (cache stability for the summarizer).
- **Cost-ledger idempotency** — `services/worker-service/core/cost_ledger_repository.py`. Summarization inside the hook still writes via the same ledger with the same `ON CONFLICT DO NOTHING` keying.

<!-- AGENT_TASK_END -->
