<!-- AGENT_TASK_START: task-8-pipeline-and-graph-integration.md -->

# Task 8 — Pipeline Orchestrator + State Schema + `agent_node` Integration

## Agent Instructions

**CRITICAL PRE-WORK:**
1. `docs/design-docs/phase-2/track-7-context-window-management.md` — sections "Architecture overview", "State schema extensions", "Checkpoint interaction", "Core design rules", and "Cross-track coordination".
2. `services/worker-service/executor/graph.py` — entire `_build_graph` method, especially `agent_node`, the state-class selection logic (`stack_enabled` + `MemoryEnabledState`), the per-step budget enforcement (Track 3 carve-out site), and the post-astream commit path.
3. `services/worker-service/executor/memory_graph.py` — precedent for extending graph state with custom reducers and for a custom state TypedDict that co-exists with `MessagesState`.
4. `services/worker-service/executor/compaction/*.py` — all of Tasks 2, 3, 4, 5, 6's outputs. Read the public surface you are about to compose.
5. `services/worker-service/core/worker.py` — post-astream cost attribution, dead-letter transitions.

**CRITICAL POST-WORK:**
1. Run `make worker-test` AND `make e2e-test`.
2. Update Task 8 status in `docs/exec-plans/active/phase-2/track-7/progress.md`.

## Context

This task integrates Tasks 2–6 into a live LangGraph-driven task. It:

1. Defines a **single unified `RuntimeState` TypedDict** with fields from both Track 5 (memory) and Track 7 (compaction). Replaces the current `MemoryEnabledState if stack_enabled else MessagesState` branching in `_build_graph`. Per LangGraph best practices research (langgraph-swarm-py, open_deep_research, chat-langchain all do this; [langgraphjs #536](https://github.com/langchain-ai/langgraphjs/issues/536) confirms LangGraph has no schema-migration API, so per-task schema swapping is on the unsupported side of the checkpointer).
2. Branches **graph topology**, not state: `memory_write` node is added iff memory is enabled; compaction pipeline always runs inside `agent_node`.
3. Exposes `compact_for_llm(state, raw_messages, agent_config, model_context_window, task_context) -> (compacted_messages, state_updates, events)` — the pipeline orchestrator.
4. Calls `compact_for_llm` from `agent_node` before every `llm_with_tools.ainvoke`.
5. Adds `compaction.tier3` to the Track 3 per-step named-node budget carve-out alongside `memory_write`.
6. Emits Langfuse spans and structured log events.

## Task-Specific Shared Contract

### `RuntimeState` — unified state schema (replaces current branching)

```python
class RuntimeState(TypedDict):
    # Core — always populated.
    messages: Annotated[list[BaseMessage], add_messages]

    # Track 5 (memory) fields — populated by memory-enabled graphs only.
    # Defaults are reducer-safe (`[]`, not `None`) because LangGraph's
    # `operator.add` raises on None. See langgraph #4305 for why we avoid
    # Non-instantiable types on reducer-backed fields (Optional, unions, etc.).
    observations: Annotated[list[str], operator.add]         # default []
    pending_memory: dict                                     # default {}
    memory_opt_in: bool                                      # default False

    # Track 7 (compaction) fields — populated by compact_for_llm.
    cleared_through_turn_index: Annotated[int, _max_reducer]             # default 0
    truncated_args_through_turn_index: Annotated[int, _max_reducer]      # default 0
    summarized_through_turn_index: Annotated[int, _max_reducer]          # default 0
    summary_marker: Annotated[str, _summary_marker_strict_append_reducer]  # default ""
    memory_flush_fired_this_task: Annotated[bool, _any_reducer]          # default False
    last_super_step_message_count: Annotated[int, _max_reducer]          # default 0
```

**Initial state construction** — at task start, `agent_node` receives a state dict with all fields present and at their reducer-safe defaults:

```python
initial_state: RuntimeState = {
    "messages": [],
    "observations": [],        # MUST be [], not None — operator.add crashes on None
    "pending_memory": {},      # MUST be {}, not None
    "memory_opt_in": False,
    "cleared_through_turn_index": 0,
    "truncated_args_through_turn_index": 0,
    "summarized_through_turn_index": 0,
    "summary_marker": "",      # MUST be "", not None — strict-append reducer expects str
    "memory_flush_fired_this_task": False,
    "last_super_step_message_count": 0,
}
```

**Why one state, not per-feature schemas:** see design doc §State schema extensions. Summary: LangGraph reducers only fire for keys present in a node's return value ([docs](https://docs.langchain.com/oss/python/langgraph/use-graph-api)), so unused fields cost nothing at runtime. The checkpointer has no schema-migration API; per-task schema swapping breaks resume / redrive / follow-up whenever agent config changes between super-steps. Every reference implementation at scale (langgraph-swarm-py, open_deep_research) uses one TypedDict with topology branching.

Where:

- `_max_reducer(a: int, b: int) -> int` returns `max(a, b)`. Monotonicity — a stale super-step that returns a lower value cannot regress the watermark.
- `_any_reducer(a: bool, b: bool) -> bool` returns `a or b`. One-shot monotonicity for the memory-flush flag.
- `_summary_marker_strict_append_reducer(a: str | None, b: str | None) -> str | None`:
  - If `b is None`, return `a` (no update).
  - If `a is None`, return `b` (first write).
  - If `b.startswith(a)`: return `b` (append — normal second-Tier-3 path).
  - Else: emit `compaction.summary_marker_non_append` structured log (no replace path exists in v1) AND return `a` (REJECT the non-append write). Non-append rewrites invalidate KV-cache on every subsequent call and violate Design §Core design rule 1. There is no legitimate replace path in v1; regenerating the marker requires explicit state clearing, which is not in scope.
  - Rollback via `rollback_last_checkpoint` restores the state snapshot wholesale outside the reducer, so rollback is not constrained by this rule.

Track 7 is always-on; there is no runtime enable/disable knob. If compaction breaks in production, the operator path is a standard deploy rollback — matches Tracks 3/4/5.

**Track 5 refactor included here.** Track 5 currently branches: `state_type = MemoryEnabledState if stack_enabled else MessagesState`. This task replaces that with `state_type = RuntimeState` unconditionally. Track 5's existing `MemoryEnabledState` class and the conditional become dead code and are removed. Memory-disabled tasks now have the same state schema as memory-enabled tasks; they simply never write to memory fields (defaults remain `[]`/`{}`/False throughout the task). Graph topology still branches — `memory_write` node is added only when memory is enabled.

### State selection in `_build_graph` (collapsed from four-way to one-way)

```python
memory_enabled = decision.stack_enabled   # unchanged — Track 5 gating

state_type = RuntimeState                 # ALWAYS
workflow = StateGraph(state_type)
workflow.add_node("agent", agent_node, input_schema=state_type)

if memory_enabled:
    workflow.add_node(MEMORY_WRITE_NODE_NAME, memory_write_graph_node)
    workflow.add_edge(MEMORY_WRITE_NODE_NAME, END)
```

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
   a. Check Task 9's pre-flush hook (if Task 9 has landed; otherwise skip). Task 8 lands first and leaves the pre-flush a no-op until Task 9 wires it.
   b. Call `summarize_slice(...)` for the newly-old slice.
   c. On success: append `summary_text` to the in-memory `summary_marker`, advance `summarized_through_turn_index`, emit `compaction.tier3_fired` event, and rebuild the compacted message list as `[SystemMessage(summary_marker), *messages[summarized_through_turn_index:]]`.
   d. On skip: emit `compaction.tier3_skipped` event; do NOT advance the watermark; leave the compacted messages from step 4 as final.
6. If estimated tokens still exceed model context window AFTER all tiers: set `events.append(HardFloorEvent(...))` but do NOT transition dead-letter from the pipeline — the caller in `agent_node` inspects the event and invokes the worker's existing dead-letter path. (Coupling back into the worker's state machine belongs in Task 10.)
7. Return `CompactionPassResult`.

**No DB writes from the pipeline itself** except the summarizer's cost ledger row (Task 7 owns that). The pipeline's role is purely to compute the compacted view + state updates + event list.

### `agent_node` integration

In `graph.py`:

```python
async def agent_node(state, config: RunnableConfig):
    messages = state["messages"]
    if not any(isinstance(m, SystemMessage) for m in messages):
        # existing system-prompt injection
        ...

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

`_serialize_for_token_count` must be deterministic (sort JSON keys, consistent message formatting) so two calls with the same messages produce the same token count — otherwise Tier 1 fires at different thresholds across retries.

**Serialization determinism across checkpoint save/load.** `AIMessage.additional_kwargs`, `response_metadata`, callback injection state, and tool-call `id` fields can drift between pre-checkpoint and post-checkpoint objects (different defaultdict ordering, stripped metadata, etc.). The serializer MUST use an explicit allow-list of fields — only `type`, `content`, `tool_calls[].name`, `tool_calls[].args` (with sorted keys), `tool_call_id` for tool messages. Everything else (IDs, response_metadata, additional_kwargs, usage_metadata) is excluded. Unit-tested via: `estimate_tokens(deserialize(serialize(msgs))) == estimate_tokens(msgs)` on a realistic checkpoint round-trip fixture. Without this, the compaction trigger boundary drifts across resume and KV-cache invalidates. Both `tiktoken` and `anthropic.count_tokens` already exist in the worker's dependency tree (Track 1 / Track 5 added them).

`provider` comes from the agent config (`agent_config.provider`).

### Model context window lookup

Read from the `models` table row for `agent_config.model` at graph-build time. Cache on the `GraphExecutor` for the lifetime of the `execute_task` invocation. For BYOT / custom models not in `models`, default to **32_000** tokens (conservative) and emit a `compaction.model_context_window_unknown` structured log at graph build so operators can notice. Never guess upward.

## Affected Component

- **Service/Module:** Worker Service — Compaction + graph
- **File paths:**
  - `services/worker-service/executor/compaction/state.py` (new — `RuntimeState` TypedDict + reducers (Task 2 already created the file; Task 8 adds Track 7 fields))
  - `services/worker-service/executor/compaction/pipeline.py` (new — `compact_for_llm`, `CompactionPassResult`, event types, `estimate_tokens`)
  - `services/worker-service/executor/compaction/tokens.py` (new — real-tokenizer-preferred token estimation for Anthropic/OpenAI; heuristic for Gemini/BYOT)

  (`compaction/gating.py` is owned by Task 3 — pure resolver with no dependency on state/pipeline internals. Task 4 and Task 8 both import it.)
  - `services/worker-service/executor/compaction/__init__.py` (modify — **SOLE OWNER** of the final public-API surface; consolidate all `compaction.*` re-exports here. Tasks 2–6 leave `__init__.py` empty-minus-docstring so the package can be imported; Task 8 fills it in. No other task edits this file.)
  - `services/worker-service/executor/graph.py` (modify — state selection, `agent_node` pipeline call, budget carve-out)
  - `services/worker-service/tests/test_compaction_pipeline.py` (new)
  - `services/worker-service/tests/test_compaction_state_reducers.py` (new)
  - `services/worker-service/tests/test_graph_compaction_integration.py` (new)
- **Change type:** two new modules + significant `graph.py` modification

## Dependencies

- **Must complete first:** Tasks 2, 3, 4, 5, 6.
- **Parallel-safe with:** none that touch `graph.py`. Task 10 (dead-letter enum) edits migration/enum files only — can proceed in parallel.
- **Provides output to:** Task 9 (extends pipeline with pre-Tier-3 flush), Task 12 (E2E tests).

## Implementation Specification

Follow the Task-Specific Shared Contract above. Additional notes:

- Preserve existing system-prompt injection inside `agent_node` (the `if not any(isinstance(m, SystemMessage))` path). Compaction runs AFTER that, so the system prompt is always on top of the compacted view.
- When `summary_marker` is non-None, prepend it as a `SystemMessage` (not a `HumanMessage`) so the model treats it as context, not user input. Attach `additional_kwargs={"compaction": True}` for Langfuse debug visibility.
- Do not fire Tier 3 on the same call that advanced Tier 1 — re-estimate token count between tiers. If Tier 1 + 1.5 together brought input below `thresholds.tier3`, Tier 3 is skipped on this call.
- Emit Langfuse spans via the callback handler: one `compaction.inline` span per call when Tier 1 or Tier 1.5 advanced, one `compaction.tier3` span wrapping the summarizer call.
- Structured log events carry `tenant_id`, `agent_id`, `task_id`, `step_index` (the count of agent-node calls so far in this task).

## Acceptance Criteria

- [ ] On a small task (few tool calls) raw message history stays below the Tier 1 threshold and does NOT fire Tier 1. `compaction.tier1_applied` is NOT logged on these calls.
- [ ] A synthetic task that pushes past the Tier 1 threshold fires `compaction.tier1_applied` exactly on the turn the threshold is first crossed. Watermark advances.
- [ ] Tier 1 and Tier 1.5 are idempotent across repeated calls — running the pipeline twice on the same state advances watermarks on the first call and is a no-op on the second.
- [ ] Tier 3 fires ONLY when Tier 1 + 1.5 cannot bring estimated input below the Tier 3 threshold.
- [ ] `summary_marker` after two Tier 3 firings has the second summary appended to the first (assert via unit test with a mocked summarizer).
- [ ] When Tier 3 skips (summarizer fails after retries), `summarized_through_turn_index` is NOT advanced and the next call re-attempts.
- [ ] `compaction.tier3` is in the Track 3 named-node budget carve-out — a task with `budget_max_per_task` close to Tier 3 cost does not pause mid-summarization. Regression test included.
- [ ] Every task on the worker — regardless of memory-enabled / memory-disabled — uses the same `RuntimeState` TypedDict. No conditional state-class selection remains in `_build_graph`.
- [ ] Memory-disabled tasks have `observations=[]`, `pending_memory={}`, `memory_opt_in=False` throughout (reducer-safe defaults); no field is ever `None`.
- [ ] Memory-enabled tasks have the `memory_write` node wired (topology branching). Memory-disabled tasks do not have it wired.
- [ ] An existing Track 5 checkpoint (written with the pre-refactor `MemoryEnabledState`) deserialises cleanly against `RuntimeState` — the compaction fields default-initialise, memory fields match their old values. Regression test loads a V1 fixture and resumes.
- [ ] Pipeline updates `last_super_step_message_count = len(raw_messages)` in every `CompactionPassResult.state_updates` so heartbeat detection in Task 9 has the watermark it needs.
- [ ] `__init__.py` after Task 8 re-exports the full public API: `KEEP_TOOL_USES`, `resolve_thresholds`, `cap_tool_result`, `clear_tool_results`, `truncate_tool_call_args`, `summarize_slice`, `compact_for_llm`, `RuntimeState`, plus all referenced result/event types. Callers import from the package root after this task lands.
- [ ] Summary marker strict-append reducer rejects non-append writes — unit test asserts a non-append second write returns the ORIGINAL marker value, and `compaction.summary_marker_non_append` is logged.
- [ ] Watermark reducers are `max` — a synthetic stale super-step returning `{cleared_through_turn_index: 0}` while state is at 10 does NOT regress the value.
- [ ] `summary_marker` reducer appends when the new value has the old as a prefix; logs `compaction.summary_marker_non_append` when a non-prefix write is rejected (strict-append reducer; replace is not allowed).
- [ ] `make worker-test` and `make e2e-test` green.

## Testing Requirements

- **Pipeline unit tests (mocked summarizer):** synthesize message lists of various shapes and lengths; assert tier-ordering, threshold gating, event emission, state updates.
- **Cache-stability invariant:** call `compact_for_llm` twice on the same state with the same mock summarizer returning a deterministic response; assert byte-identical `messages` and state_updates from both calls.
- **State reducer tests:** `max` reducer rejects regressions; `any` reducer for the memory-flush flag; summary_marker append vs replace.
- **Opt-out parity tests:** every existing test in `test_graph_*` that runs with stock `MessagesState` MUST still pass when Track 7 is disabled.
- **Integration test (with a real LangGraph compiled graph + mocked LLM):** a synthetic task runs 10 tool calls; Tier 1 fires at the right moment; watermarks advance; no memory-flush fires (Task 9 not yet wired in this task).
- **Budget carve-out test:** construct a task with `budget_max_per_task = Tier-3-cost + 1` microdollar; force Tier 3; assert the task does not pause.

## Constraints and Guardrails

- Do not change tool-execution paths — Task 4 already wraps them with the cap.
- Do not emit Langfuse spans or structured logs from `compact_for_llm` itself — the function returns events in `CompactionPassResult`; the caller (`agent_node`) emits them. This makes the pipeline testable without mocking the logger.
- Do not load balance summarizer calls across multiple models — one call per Tier 3 firing.
- Do not persist any Track 7 state outside LangGraph checkpoints — all of it lives in graph state.
- Do not invoke Task 9's pre-flush here. Leave an `if pre_flush_should_fire(...):` hook with a `pass` body; Task 9 fills it in.
- Do not dead-letter from the pipeline — surface `HardFloorEvent` to the caller; caller handles the transition via the existing dead-letter API.

## Assumptions

- Track 5 is already live (`MemoryEnabledState` exists and works). If Track 7 lands before Track 5, `RuntimeState` already has the unified schema from Task 2; Task 8 only adds Track 7 fields to it.
- Track 3's per-step budget enforcement is already identifiable in `graph.py`. If not present, surface this back to the orchestrator — Track 3 is a dependency.
- LangChain `add_messages` and TypedDict-with-Annotated reducer pattern work on the worker's pinned Python + LangGraph versions (Track 5 proved this).

<!-- AGENT_TASK_END: task-8-pipeline-and-graph-integration.md -->
