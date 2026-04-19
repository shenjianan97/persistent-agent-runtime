<!-- AGENT_TASK_START: task-2-state-schema-unification.md -->

# Task 2 — State Schema Unification (Pre-Refactor, Zero Behavior Change)

## Agent Instructions

You are a software engineer implementing one module of a larger system. Your scope is strictly limited to this task.

**This is a REFACTOR, not a feature.** Nothing about agent behavior, LLM output, memory write, or tool wrapping changes. The sole deliverable is: every task on the worker uses the same `RuntimeState` TypedDict regardless of memory-enabled/disabled status. The current binary `MemoryEnabledState if stack_enabled else MessagesState` selection goes away. All existing Track 5 tests continue to pass.

**CRITICAL PRE-WORK:**
1. `docs/design-docs/phase-2/track-7-context-window-management.md` — section "State schema extensions" (the unified-`RuntimeState` discussion).
2. `services/worker-service/executor/graph.py` — find `_build_graph` around line 599–601; that's the current branching site:
   ```python
   state_type = MemoryEnabledState if stack_enabled else MessagesState
   workflow = StateGraph(state_type)
   workflow.add_node("agent", agent_node, input_schema=state_type)
   ```
3. `services/worker-service/executor/memory_graph.py` — the current `MemoryEnabledState` TypedDict definition. Note which fields + reducers are on it.
4. LangGraph research findings:
   - Reducers only fire for keys present in a node's return value ([docs](https://docs.langchain.com/oss/python/langgraph/use-graph-api)) — unused fields cost nothing at runtime.
   - LangGraph has no schema-migration API ([langgraphjs #536](https://github.com/langchain-ai/langgraphjs/issues/536)); per-task schema swapping is on the unsupported side of the checkpointer.
   - `Optional[T]` bypasses custom reducers in v0.3.30 ([langgraph #4305](https://github.com/langchain-ai/langgraph/issues/4305)) — always use direct types + reducer-safe sentinel defaults (`[]`, `{}`, `""`, `0`, `False`).
   - `operator.add` crashes on `None` — lists must default to `[]`.
5. All Track 5 tests under `services/worker-service/tests/` that reference `MemoryEnabledState` or assume memory-disabled tasks use `MessagesState`. Identify them via `grep -rn MemoryEnabledState services/worker-service`.

**CRITICAL POST-WORK:**
1. `make test` — Java tests unaffected; should all pass.
2. `make worker-test` — every Python test including Track 5's full suite MUST pass with zero changes to test logic. If a test needs to change, that's a red flag — the test was asserting on schema shape, not behavior. Discuss before modifying.
3. `make e2e-test` — integration path exercising the refactor; all existing Track 5 ACs must still hold.
4. Orchestrator must run Playwright Scenarios 1, 11, 12, 13 (Track 5's browser suite) to confirm nothing customer-visible changed.
5. Update Task 2 status in `docs/exec-plans/active/phase-2/track-7/progress.md`.

## Context

Today's worker branches the graph state schema per task:

```python
stack_enabled = decision.stack_enabled
state_type = MemoryEnabledState if stack_enabled else MessagesState
workflow = StateGraph(state_type)
```

This means memory-enabled tasks carry the `MemoryEnabledState` extra fields (`observations`, `pending_memory`, `memory_opt_in`) while memory-disabled tasks carry only `MessagesState`. The schema is a function of config, not code.

This pattern breaks when Track 7 adds more fields. More importantly, it breaks *today* for any Track 5 task where `agent.memory.enabled` is toggled between super-steps — the next resume after the flip uses the new schema, and the checkpointed fields from the old schema either disappear (downgrade) or stay uninitialised (upgrade). LangGraph has no schema-migration API; this is silently broken.

Fix it before Track 7 lands. Move to a unified `RuntimeState` that contains the union of all fields from all features. Reducers on fields that a given task doesn't use simply never fire (per LangGraph docs, reducers only execute for keys returned by a node). Memory-disabled tasks keep defaults `[]`/`{}`/`False` in their memory fields — harmless and cheap.

## Task-Specific Shared Contract

Define `RuntimeState` in a new module `executor/compaction/state.py` even though Track 7 hasn't started shipping yet. Rationale: this is the forward-looking home of the state schema; Track 5 fields live there now so Task 8's compaction field additions later are purely additive. **At the end of this task**, `compaction/state.py` contains *only the Track 5 fields plus `messages`* — no Track 7 fields yet. Task 8 will add them later.

**`RuntimeState` after Task 2 (Track 5 fields only):**

```python
from typing import Annotated, TypedDict
import operator
from langchain_core.messages import BaseMessage
from langgraph.graph import add_messages


class RuntimeState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]

    # Track 5 (memory) fields — populated by memory-enabled graphs only.
    # Defaults are reducer-safe: [] not None (operator.add crashes on None),
    # {} not None, False not None. Direct types — no Optional[T] — to avoid
    # the reducer-bypass bug in langgraph #4305.
    observations: Annotated[list[str], operator.add]
    pending_memory: dict
    memory_opt_in: bool
```

**`MemoryEnabledState` becomes a deprecated alias** (or is deleted outright; see below).

### Required code changes

1. **Create `services/worker-service/executor/compaction/__init__.py`** (docstring-only, Task 3 fills it in later).
2. **Create `services/worker-service/executor/compaction/state.py`** with `RuntimeState` as above.
3. **Modify `services/worker-service/executor/memory_graph.py`:**
   - Replace the `MemoryEnabledState` TypedDict definition with either:
     - (a) `from executor.compaction.state import RuntimeState` + `MemoryEnabledState = RuntimeState` as a backward-compat alias, OR
     - (b) Delete `MemoryEnabledState` entirely and update all import sites to use `RuntimeState`.
   - Prefer (b) — cleaner, forces the migration to propagate through the codebase once, no dangling alias to delete later. Use (a) only if there are callers outside this repo (there aren't).
4. **Modify `services/worker-service/executor/graph.py` around the current branching site (line 599):**
   ```python
   # Before
   state_type = MemoryEnabledState if stack_enabled else MessagesState
   # After
   state_type = RuntimeState
   ```
   Remove the `stack_enabled`-based branching for the state type. `stack_enabled` itself remains — it still gates *topology* (whether the `memory_write` node is added, whether memory tools are registered) — only the *schema* is unified.
5. **Audit initial-state construction.** Every call site that builds an initial graph input must now supply all `RuntimeState` fields with reducer-safe defaults. Most likely this is in `execute_task` where the graph is kicked off. Provide:
   ```python
   initial_input: RuntimeState = {
       "messages": messages,
       "observations": [],
       "pending_memory": {},
       "memory_opt_in": False,
   }
   ```
   Even for memory-disabled tasks — LangGraph tolerates extra keys, and having them default-initialised is what makes the "never used" path zero-cost.
6. **Audit all test fixtures.** Every `MemoryEnabledState` reference in `services/worker-service/tests/` becomes `RuntimeState`. Every `MessagesState`-based test for memory-disabled tasks either switches to `RuntimeState` with defaults or stays on `MessagesState` if the test was asserting on the stock type directly (those are pre-existing tests from before Track 5; leave them alone).

### What does NOT change

- **LLM output**, **memory write behavior**, **memory tool availability**, **task completion path** — all unchanged. Every customer-visible and agent-visible behavior must be bit-identical post-refactor.
- **Track 5's `memory_write` graph node** and its wiring — unchanged. The node still reads `observations`, writes `pending_memory`, executes on the terminal branch.
- **Memory-enabled gating** on tool registration (`memory_note`, `memory_search`, `task_history_get`) — unchanged. The gate is `agent.memory.enabled`, not the state schema.
- **`stack_enabled` and `auto_write` flags** from the Track 5 decision object — still used, still gate topology.
- **Checkpointer schema version** — LangGraph doesn't expose one, and the refactor is backward-compatible (old checkpoints have fewer keys than `RuntimeState`; TypedDict tolerates missing keys; field-level defaults fill in on access).

### Backward compatibility with existing Track 5 checkpoints

An in-flight task checkpointed before this refactor has state in the shape of the old `MemoryEnabledState` (which was the same fields as new `RuntimeState`). When the refactored worker resumes it, the state deserialises cleanly. Memory-disabled tasks checkpointed as `MessagesState` (only `messages`) also deserialise cleanly — the missing `observations`/`pending_memory`/`memory_opt_in` fields become absent dict keys, and every consumer that reads them uses `.get(..., default)` or applies the initial-input construction. Verify this explicitly with a checkpoint-fixture regression test.

## Affected Component

- **Service/Module:** Worker Service — executor, memory subgraph
- **File paths:**
  - `services/worker-service/executor/compaction/__init__.py` (new — docstring-only)
  - `services/worker-service/executor/compaction/state.py` (new — `RuntimeState`)
  - `services/worker-service/executor/memory_graph.py` (modify — remove `MemoryEnabledState` class, update imports)
  - `services/worker-service/executor/graph.py` (modify — remove state-type branching around line 599)
  - `services/worker-service/core/worker.py` (audit — initial-state construction at `execute_task` kickoff)
  - `services/worker-service/tests/test_memory_graph_*.py`, `tests/test_graph_*.py`, any other test referencing `MemoryEnabledState` (modify — import swap)
  - `services/worker-service/tests/test_runtime_state_schema.py` (new — regression fixtures)
- **Change type:** pure refactor + new-module move of the TypedDict + test audit

## Dependencies

- **Must complete first:** None. This task can start in parallel with Task 1 (Java API).
- **Blocks:** Tasks 3, 4, 5, 6, 7, 8, 9 (all worker-side Track 7 tasks). No worker-side feature task begins until this refactor is green.
- **Shared interfaces/contracts:** `RuntimeState` TypedDict. Published location: `executor/compaction/state.py`. Every downstream task imports from there.

## Implementation Specification

Follow the steps under §Task-Specific Shared Contract in order:

1. New package + module skeleton.
2. Define `RuntimeState`.
3. Update `memory_graph.py` imports + delete old TypedDict.
4. Update `graph.py` branching site.
5. Update initial-state construction in `core/worker.py`.
6. Audit tests — `grep -rn MemoryEnabledState services/worker-service` must return zero hits post-refactor.
7. Run full test suite; fix any failure by investigating whether the test was asserting *behavior* (must fix the code) or *schema shape* (must fix the test).

No new logic. No new reducers. No new behavior. The PR diff is almost entirely imports and the TypedDict move.

## Acceptance Criteria

- [ ] `grep -rn MemoryEnabledState services/worker-service` returns zero hits after the refactor.
- [ ] `_build_graph` in `graph.py` no longer contains `state_type = MemoryEnabledState if stack_enabled else MessagesState` (or any variant). The single line is `state_type = RuntimeState`.
- [ ] Memory-enabled tasks behave identically to pre-refactor: `memory_write` node fires on terminal branch, `memory_note` / `memory_search` / `task_history_get` tools are registered, memory REST endpoints return the same data.
- [ ] Memory-disabled tasks behave identically to pre-refactor: no `memory_write` node in the graph, no memory tools registered, no `agent_memory_entries` row written.
- [ ] Initial-state construction at task kickoff provides all `RuntimeState` keys with reducer-safe defaults (`[]`, `{}`, `False`) — even for memory-disabled tasks.
- [ ] `make worker-test` — every Python test passes, zero test-logic changes beyond import renames.
- [ ] `make e2e-test` — all Track 5 and earlier E2E tests pass.
- [ ] **Checkpoint-regression test** at `services/worker-service/tests/test_runtime_state_schema.py`:
  - Load a synthetic pre-refactor `MemoryEnabledState` checkpoint fixture; resume the graph; assert the task completes with identical state as a control run from the refactored code.
  - Load a synthetic pre-refactor `MessagesState` checkpoint fixture (memory-disabled); assert the same.
- [ ] **Reducer-safety unit tests:** invoke `operator.add` on the `observations` field with `new=["x"]` against an initial state — must succeed (default `[]` + `["x"]` = `["x"]`). The same with `None` as the initial value must FAIL loudly — confirming the task correctly initialises to `[]`.
- [ ] `stack_enabled` and `auto_write` still gate memory topology correctly — `memory_write` node is wired iff `stack_enabled` is True, regardless of state schema.
- [ ] Orchestrator (post-merge) runs Playwright Scenarios 1, 11, 12, 13 and all pass. Subagent does NOT run Playwright per AGENTS.md §Browser Verification.

## Testing Requirements

- **Unit tests** — audit existing Track 5 tests for `MemoryEnabledState` references; rename imports to `RuntimeState`. Assert the unchanged test passes.
- **Checkpoint backward-compat fixtures** — pre-refactor checkpoint JSON for both memory-enabled and memory-disabled tasks; load-and-resume passes without key-error.
- **Reducer-safety** — direct unit tests on `operator.add([], ["x"])` and the `_max_reducer`-equivalent behaviors (will matter for later tasks, but this task includes the scaffolding so Task 8 doesn't duplicate it).
- **Zero-behavior-change E2E** — full Track 5 test suite passes. `make worker-test` green before this task is marked Done.

## Constraints and Guardrails

- **Do NOT add Track 7 fields** (`cleared_through_turn_index`, `summary_marker`, etc.) to `RuntimeState` in this task. That's Task 8. Task 2's `RuntimeState` is Track-5-fields-only.
- **Do NOT change tests' assertions** on behavior. If a test fails post-refactor, investigate — it almost certainly reveals a behavior regression, not a schema-shape mismatch.
- **Do NOT use `Optional[T]`** anywhere in the new `RuntimeState`. Direct types with reducer-safe defaults only.
- **Do NOT introduce a schema-version number.** Append-only evolution is the discipline; versioning is an unsolved problem in LangGraph and we don't own that layer.
- **Do NOT skip the orchestrator Playwright run.** Track 5 shipped with a browser-verified Console surface; this refactor touches the worker, but a regression in memory-write behavior is customer-visible on the Memory tab. Orchestrator owns the browser check.
- **Do NOT delete or rename any Track 5-era public symbol that is imported from another service.** Internal-only refactor. `grep` outside `services/worker-service/` for any import of `MemoryEnabledState`; if found, resolve before renaming.

## Assumptions

- LangGraph version pinned in `pyproject.toml` supports `TypedDict` with `Annotated` reducers (Track 5 proved this).
- `operator.add` on `list[str]` works with `[]` as the default (stdlib behavior — confirmed).
- LangGraph's `add_messages` reducer on `messages` is unchanged from Track 5's usage.
- The `stack_enabled` and `auto_write` decision fields come from Track 5's `decide()` helper — unchanged by this task.

<!-- AGENT_TASK_END: task-2-state-schema-unification.md -->
