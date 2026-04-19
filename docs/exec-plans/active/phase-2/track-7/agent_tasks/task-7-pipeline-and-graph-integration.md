<!-- AGENT_TASK_START: task-7-pipeline-and-graph-integration.md -->

# Task 7 — Pipeline Orchestrator + State Schema + `agent_node` Integration

## Agent Instructions

**CRITICAL PRE-WORK:**
1. `docs/design-docs/phase-2/track-7-context-window-management.md` — sections "Architecture overview", "State schema extensions", "Checkpoint interaction", "Core design rules", and "Cross-track coordination".
2. `services/worker-service/executor/graph.py` — entire `_build_graph` method, especially `agent_node`, the state-class selection logic (`stack_enabled` + `MemoryEnabledState`), the per-step budget enforcement (Track 3 carve-out site), and the post-astream commit path.
3. `services/worker-service/executor/memory_graph.py` — precedent for extending graph state with custom reducers and for a custom state TypedDict that co-exists with `MessagesState`.
4. `services/worker-service/executor/compaction/*.py` — all of Tasks 2, 3, 4, 5, 6's outputs. Read the public surface you are about to compose.
5. `services/worker-service/core/worker.py` — post-astream cost attribution, dead-letter transitions.

**CRITICAL POST-WORK:**
1. Run `make worker-test` AND `make e2e-test`. **Mandatory**: confirm that agents with `context_management.enabled=false` are behaviourally identical to pre-Track-7 (no new state fields, no pipeline invocation, no new log events, no new cost rows). This is the opt-out correctness gate.
2. Update Task 7 status in `docs/exec-plans/active/phase-2/track-7/progress.md`.

## Context

This task integrates Tasks 2–6 into a live LangGraph-driven task. It:

1. Defines `CompactionEnabledState` with monotone reducers.
2. Wires state selection so `MessagesState` is used when both Track 5 and Track 7 are disabled, `MemoryEnabledState` when only Track 5, `CompactionEnabledState` when only Track 7, and a merged state when both.
3. Exposes `compact_for_llm(state, raw_messages, agent_config, model_context_window, task_context) -> (compacted_messages, state_updates, events)` — the pipeline orchestrator.
4. Calls `compact_for_llm` from `agent_node` before every `llm_with_tools.ainvoke`.
5. Adds `compaction.tier3` to the Track 3 per-step named-node budget carve-out alongside `memory_write`.
6. Emits Langfuse spans and structured log events.

## Task-Specific Shared Contract

### `CompactionEnabledState`

```python
class CompactionEnabledState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]
    cleared_through_turn_index: Annotated[int, _max_reducer]
    truncated_args_through_turn_index: Annotated[int, _max_reducer]
    summarized_through_turn_index: Annotated[int, _max_reducer]
    summary_marker: Annotated[str | None, _summary_marker_strict_append_reducer]
    memory_flush_fired_this_task: Annotated[bool, _any_reducer]
    last_super_step_message_count: Annotated[int, _max_reducer]
```

Where:

- `_max_reducer(a: int, b: int) -> int` returns `max(a, b)`. Monotonicity — a stale super-step that returns a lower value cannot regress the watermark.
- `_any_reducer(a: bool, b: bool) -> bool` returns `a or b`. One-shot monotonicity for the memory-flush flag.
- `_summary_marker_strict_append_reducer(a: str | None, b: str | None) -> str | None`:
  - If `b is None`, return `a` (no update).
  - If `a is None`, return `b` (first write).
  - If `b.startswith(a)`: return `b` (append — normal second-Tier-3 path).
  - Else: emit `compaction.summary_marker_non_append` structured log AND return `a` (REJECT the non-append write). Non-append rewrites invalidate KV-cache on every subsequent call and violate Design §Core design rule 1. There is no legitimate replace path in v1; regenerating the marker requires explicit state clearing, which is not in scope.
  - Rollback via `rollback_last_checkpoint` restores the state snapshot wholesale outside the reducer, so rollback is not constrained by this rule.

### State selection in `_build_graph`

Replace the current two-way selection (`MessagesState` / `MemoryEnabledState`) with a four-way selection:

```python
memory_enabled = decision.stack_enabled
compaction_enabled = (agent_config.get("context_management") or {}).get("enabled", True)

if memory_enabled and compaction_enabled:
    state_type = RuntimeState   # MemoryEnabledState + CompactionEnabledState fields merged
elif memory_enabled:
    state_type = MemoryEnabledState
elif compaction_enabled:
    state_type = CompactionEnabledState
else:
    state_type = MessagesState
```

`RuntimeState` is a combined TypedDict in `compaction/state.py` that inherits both sets of fields with the correct reducer annotations on each.

### `compact_for_llm`

Location: `services/worker-service/executor/compaction/pipeline.py`.

```python
async def compact_for_llm(
    raw_messages: list[BaseMessage],
    state: Mapping[str, Any],                  # read-only view of graph state
    agent_config: Mapping[str, Any],
    model_context_window: int,
    task_context: TaskContext,                 # tenant/agent/task/checkpoint ids + cost_ledger + callbacks
    summarizer_factory: SummarizerFactory,     # returns the summarizer model id + callable
    *,
    estimate_tokens: Callable[[list[BaseMessage]], int],
) -> CompactionPassResult
```

`CompactionPassResult`:

```python
@dataclass(frozen=True)
class CompactionPassResult:
    messages: list[BaseMessage]                # Compacted view to send to the LLM
    state_updates: dict[str, Any]              # Fields to merge into state (watermarks, summary_marker, memory_flush_fired_this_task)
    events: list[CompactionEvent]              # Structured-log events (emitted by caller)
    tier3_skipped: bool                        # True if Tier 3 hit trigger but summarizer failed
```

Pipeline logic:

1. Resolve thresholds: `thresholds = resolve_thresholds(model_context_window)`.
2. Compute `est_input_tokens = estimate_tokens(raw_messages)`.
3. Apply Tier 1 (`clear_tool_results`) if `est_input_tokens > thresholds.tier1`. Record event if watermark advanced. Update `est_input_tokens` after.
4. Apply Tier 1.5 (`truncate_tool_call_args`) if `est_input_tokens > thresholds.tier1`. Same event handling.
5. If `est_input_tokens > thresholds.tier3`:
   a. Check Task 8's pre-flush hook (if Task 8 has landed; otherwise skip). Task 7 lands first and leaves the pre-flush a no-op until Task 8 wires it.
   b. Call `summarize_slice(...)` for the newly-old slice.
   c. On success: append `summary_text` to the in-memory `summary_marker`, advance `summarized_through_turn_index`, emit `compaction.tier3_fired` event, and rebuild the compacted message list as `[SystemMessage(summary_marker), *messages[summarized_through_turn_index:]]`.
   d. On skip: emit `compaction.tier3_skipped` event; do NOT advance the watermark; leave the compacted messages from step 4 as final.
6. If estimated tokens still exceed model context window AFTER all tiers: set `events.append(HardFloorEvent(...))` but do NOT transition dead-letter from the pipeline — the caller in `agent_node` inspects the event and invokes the worker's existing dead-letter path. (Coupling back into the worker's state machine belongs in Task 9.)
7. Return `CompactionPassResult`.

**No DB writes from the pipeline itself** except the summarizer's cost ledger row (Task 6 owns that). The pipeline's role is purely to compute the compacted view + state updates + event list.

### `agent_node` integration

In `graph.py`:

```python
async def agent_node(state, config: RunnableConfig):
    messages = state["messages"]
    if not any(isinstance(m, SystemMessage) for m in messages):
        # existing system-prompt injection
        ...

    if compaction_enabled:
        pass_result = await compact_for_llm(
            raw_messages=messages,
            state=state,
            agent_config=agent_config,
            model_context_window=model_context_window,
            task_context=task_context,
            summarizer_factory=self._build_summarizer_factory(...),
            estimate_tokens=estimate_tokens_fn,
        )
        messages_for_llm = pass_result.messages
        state_updates = pass_result.state_updates
        for ev in pass_result.events:
            ev.log()
    else:
        messages_for_llm = messages
        state_updates = {}

    # existing rate-limit retry loop
    response = await self._await_or_cancel(
        llm_with_tools.ainvoke(messages_for_llm, config),
        ...
    )
    return {"messages": [response], **state_updates}
```

### Budget carve-out

Locate the Track 3 per-step budget enforcement (`_check_budget_and_pause` in `graph.py` or equivalent). Add `"compaction.tier3"` to the named-node carve-out list alongside `memory_write`. Match whatever mechanism Track 5 used (name-based skip for per-task pause check; hourly-spend accounting still applies).

### Token estimation (`compaction/tokens.py`)

Real-tokenizer-preferred. Per Design §Tokens vs bytes — heuristic-only is unacceptable for Anthropic and OpenAI because code/JSON-heavy history is exactly where `len/3.5` under-counts by 30–50%, pushing Tier 3 past the provider's hard ceiling.

```python
def estimate_tokens(messages: list[BaseMessage], provider: str) -> int:
    serialized = _serialize_for_token_count(messages)  # deterministic, content+tool_calls
    if provider == "anthropic":
        import anthropic  # lazy to avoid import at startup
        return anthropic.Anthropic().count_tokens(serialized)
    if provider == "openai":
        import tiktoken
        enc = tiktoken.get_encoding("cl100k_base")
        return len(enc.encode(serialized))
    # Gemini / Google / BYOT / unknown: fall back to char-count heuristic.
    # Tolerates ±30%. If a specific provider proves persistently inaccurate,
    # add its real tokenizer above.
    return len(serialized) // 3
```

`_serialize_for_token_count` must be deterministic (sort JSON keys, consistent message formatting) so two calls with the same messages produce the same token count — otherwise Tier 1 fires at different thresholds across retries. Both `tiktoken` and `anthropic.count_tokens` already exist in the worker's dependency tree (Track 1 / Track 5 added them).

`provider` comes from the agent config (`agent_config.provider`).

### Model context window lookup

Read from the `models` table row for `agent_config.model` at graph-build time. Cache on the `GraphExecutor` for the lifetime of the `execute_task` invocation. For BYOT / custom models not in `models`, default to **32_000** tokens (conservative) and emit a `compaction.model_context_window_unknown` structured log at graph build so operators can notice. Never guess upward.

## Affected Component

- **Service/Module:** Worker Service — Compaction + graph
- **File paths:**
  - `services/worker-service/executor/compaction/state.py` (new — `CompactionEnabledState`, `RuntimeState`, reducers)
  - `services/worker-service/executor/compaction/pipeline.py` (new — `compact_for_llm`, `CompactionPassResult`, event types, `estimate_tokens`)
  - `services/worker-service/executor/compaction/tokens.py` (new — real-tokenizer-preferred token estimation for Anthropic/OpenAI; heuristic for Gemini/BYOT)
  - `services/worker-service/executor/compaction/__init__.py` (modify — **SOLE OWNER** of the final public-API surface; consolidate all `compaction.*` re-exports here. Tasks 2–6 leave `__init__.py` empty-minus-docstring so the package can be imported; Task 7 fills it in. No other task edits this file.)
  - `services/worker-service/executor/graph.py` (modify — state selection, `agent_node` pipeline call, budget carve-out)
  - `services/worker-service/tests/test_compaction_pipeline.py` (new)
  - `services/worker-service/tests/test_compaction_state_reducers.py` (new)
  - `services/worker-service/tests/test_graph_compaction_integration.py` (new)
- **Change type:** two new modules + significant `graph.py` modification

## Dependencies

- **Must complete first:** Tasks 2, 3, 4, 5, 6.
- **Parallel-safe with:** none that touch `graph.py`. Task 9 (dead-letter enum) edits migration/enum files only — can proceed in parallel.
- **Provides output to:** Task 8 (extends pipeline with pre-Tier-3 flush), Task 11 (E2E tests).

## Implementation Specification

Follow the Task-Specific Shared Contract above. Additional notes:

- Preserve existing system-prompt injection inside `agent_node` (the `if not any(isinstance(m, SystemMessage))` path). Compaction runs AFTER that, so the system prompt is always on top of the compacted view.
- When `summary_marker` is non-None, prepend it as a `SystemMessage` (not a `HumanMessage`) so the model treats it as context, not user input. Attach `additional_kwargs={"compaction": True}` for Langfuse debug visibility.
- Do not fire Tier 3 on the same call that advanced Tier 1 — re-estimate token count between tiers. If Tier 1 + 1.5 together brought input below `thresholds.tier3`, Tier 3 is skipped on this call.
- Emit Langfuse spans via the callback handler: one `compaction.inline` span per call when Tier 1 or Tier 1.5 advanced, one `compaction.tier3` span wrapping the summarizer call.
- Structured log events carry `tenant_id`, `agent_id`, `task_id`, `step_index` (the count of agent-node calls so far in this task).

## Acceptance Criteria

- [ ] Agents with `context_management.enabled=false` produce the pre-Track-7 behavior exactly: no state extension, no pipeline invocation, no new log lines, no new cost ledger rows. Verified via a regression test run with the flag toggled.
- [ ] Agents with `context_management.enabled=true` (default) and a small task (few tool calls) produce raw message history below the Tier 1 threshold and do NOT fire Tier 1. `compaction.tier1_applied` is NOT logged on these calls.
- [ ] A synthetic task that pushes past the Tier 1 threshold fires `compaction.tier1_applied` exactly on the turn the threshold is first crossed. Watermark advances.
- [ ] Tier 1 and Tier 1.5 are idempotent across repeated calls — running the pipeline twice on the same state advances watermarks on the first call and is a no-op on the second.
- [ ] Tier 3 fires ONLY when Tier 1 + 1.5 cannot bring estimated input below the Tier 3 threshold.
- [ ] `summary_marker` after two Tier 3 firings has the second summary appended to the first (assert via unit test with a mocked summarizer).
- [ ] When Tier 3 skips (summarizer fails after retries), `summarized_through_turn_index` is NOT advanced and the next call re-attempts.
- [ ] `compaction.tier3` is in the Track 3 named-node budget carve-out — a task with `budget_max_per_task` close to Tier 3 cost does not pause mid-summarization. Regression test included.
- [ ] When both Track 5 and Track 7 are enabled, the graph uses the merged `RuntimeState` — all of memory's fields AND all of compaction's fields are present in the state.
- [ ] When only Track 5 is enabled, the graph uses `MemoryEnabledState` (pre-Track-7 behavior).
- [ ] When neither is enabled, the graph uses `MessagesState` (pre-Phase-2 behavior).
- [ ] Pipeline updates `last_super_step_message_count = len(raw_messages)` in every `CompactionPassResult.state_updates` so heartbeat detection in Task 8 has the watermark it needs.
- [ ] `__init__.py` after Task 7 re-exports the full public API: `KEEP_TOOL_USES`, `resolve_thresholds`, `cap_tool_result`, `clear_tool_results`, `truncate_tool_call_args`, `summarize_slice`, `compact_for_llm`, `CompactionEnabledState`, `RuntimeState`, plus all referenced result/event types. Callers import from the package root after this task lands.
- [ ] Summary marker strict-append reducer rejects non-append writes — unit test asserts a non-append second write returns the ORIGINAL marker value, and `compaction.summary_marker_non_append` is logged.
- [ ] Watermark reducers are `max` — a synthetic stale super-step returning `{cleared_through_turn_index: 0}` while state is at 10 does NOT regress the value.
- [ ] `summary_marker` reducer appends when the new value has the old as a prefix; logs `compaction.summary_marker_replaced` when it replaces.
- [ ] `make worker-test` and `make e2e-test` green.

## Testing Requirements

- **Pipeline unit tests (mocked summarizer):** synthesize message lists of various shapes and lengths; assert tier-ordering, threshold gating, event emission, state updates.
- **Cache-stability invariant:** call `compact_for_llm` twice on the same state with the same mock summarizer returning a deterministic response; assert byte-identical `messages` and state_updates from both calls.
- **State reducer tests:** `max` reducer rejects regressions; `any` reducer for the memory-flush flag; summary_marker append vs replace.
- **Opt-out parity tests:** every existing test in `test_graph_*` that runs with stock `MessagesState` MUST still pass when Track 7 is disabled.
- **Integration test (with a real LangGraph compiled graph + mocked LLM):** a synthetic task runs 10 tool calls; Tier 1 fires at the right moment; watermarks advance; no memory-flush fires (Task 8 not yet wired in this task).
- **Budget carve-out test:** construct a task with `budget_max_per_task = Tier-3-cost + 1` microdollar; force Tier 3; assert the task does not pause.

## Constraints and Guardrails

- Do not change tool-execution paths — Task 3 already wraps them with the cap.
- Do not emit Langfuse spans or structured logs from `compact_for_llm` itself — the function returns events in `CompactionPassResult`; the caller (`agent_node`) emits them. This makes the pipeline testable without mocking the logger.
- Do not load balance summarizer calls across multiple models — one call per Tier 3 firing.
- Do not persist any Track 7 state outside LangGraph checkpoints — all of it lives in graph state.
- Do not invoke Task 8's pre-flush here. Leave an `if pre_flush_should_fire(...):` hook with a `pass` body; Task 8 fills it in.
- Do not dead-letter from the pipeline — surface `HardFloorEvent` to the caller; caller handles the transition via the existing dead-letter API.

## Assumptions

- Track 5 is already live (`MemoryEnabledState` exists and works). If Track 7 lands before Track 5, `RuntimeState` reduces to just `CompactionEnabledState` fields.
- Track 3's per-step budget enforcement is already identifiable in `graph.py`. If not present, surface this back to the orchestrator — Track 3 is a dependency.
- LangChain `add_messages` and TypedDict-with-Annotated reducer pattern work on the worker's pinned Python + LangGraph versions (Track 5 proved this).

<!-- AGENT_TASK_END: task-7-pipeline-and-graph-integration.md -->
