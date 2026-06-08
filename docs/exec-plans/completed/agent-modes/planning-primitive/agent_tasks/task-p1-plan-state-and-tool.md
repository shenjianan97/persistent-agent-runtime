<!-- AGENT_TASK_START: task-p1-plan-state-and-tool.md -->

# Task P1 — Plan State + `plan_write` Tool (Planning Primitive)

## Agent Instructions

You are a software engineer implementing one module of a larger system. Your scope is strictly limited to this task.

**CRITICAL PRE-WORK:** Before beginning implementation, you MUST read:
1. `docs/design-docs/agent-modes/design.md` — sections **"How Planning Primitive composes"** (the Planning Primitive owns *only* the ReAct agent scratchpad; it is a self-reminder, not load-bearing) and **"What stays open for the Planning Primitive's own design pass"** (write semantics, rendering format, and size limits are the open questions this task resolves for v1).
2. `docs/exec-plans/active/agent-modes/planning-primitive/plan.md` — §A0 point **5** (injected post-compaction with neutral framing for this track), §A1.1 (the P1 overview item), §A2 row "`plan` state + `plan_write`", §A1.1 + §B row **P1** (output contract — `RuntimeState.plan` list + full-list-replace reducer + `plan_write` built-in tool + allowlist-gated + written verbatim), §A5 risk "Plan grows unbounded" (P1 picks v1 caps and documents them).
3. `services/worker-service/executor/compaction/state.py` — the `RuntimeState` TypedDict (`class RuntimeState`, fields end ~`:209`) and the existing custom reducers: `_max_reducer`, `_any_reducer`, `_list_replace_reducer` (`:70`), `_summary_replace_reducer` (`:83`). Note the design-note comment block (no `Optional[T]` on reducer-annotated fields; reducer-safe sentinel defaults) — your new field follows the same discipline. `_list_replace_reducer`'s docstring is the precedent for replace-semantics + cache-stability framing.
4. `services/worker-service/executor/graph.py` — `_get_tools` (`:850`–`:1057`): how built-in tools are conditionally registered behind an `if "<name>" in allowed_tools:` allowlist gate and appended as `StructuredTool.from_function(...)`. Also note `llm.bind_tools` (`:1269`) binds the assembled list. Pick an existing tool's registration block (e.g. `web_search` at `:876`) as the structural pattern.
5. `services/worker-service/tools/definitions.py` — existing built-in tool definitions and the `interrupt()` tool at `:449` as a pattern for a tool that writes into graph state (returns a `Command`-shaped state update) rather than returning a plain string.

**SHARED-FILE / WORKTREE WARNING:** This task edits `executor/compaction/state.py` (adds the `plan` field + reducer) and `executor/graph.py::_get_tools` (registers `plan_write`). **The Supervisor Topology track (S3/S4) also touches both files.** Per AGENTS.md §Parallel Subagent Safety and plan.md §A3, if any Supervisor Topology task runs in parallel, **use `isolation: "worktree"`** for at least one side and merge after. Land this `RuntimeState`/`_get_tools` extension green before parallel tool work where possible.

**CRITICAL POST-WORK:** After completing this task:
1. Run the narrowest worker tests that cover the change (NOT the whole suite). Use the pinned venv: `services/worker-service/.venv/bin/python -m pytest services/worker-service/tests/<your_test_file>.py`. The reducer + tool are pure unit-testable with no infra. If you add a graph-registration assertion that needs the DB, run it via `make e2e-test PYTEST_ARGS='-k <your_test>'` on the isolated harness — never raw `pytest tests/backend-integration` in a worktree.
2. Update the status of P1 in `docs/exec-plans/active/agent-modes/planning-primitive/progress.md` to "Done".

## Context

The Planning Primitive gives a ReAct agent a durable scratchpad: a flat list of plan items the agent rewrites whenever it calls `plan_write`. The plan is **not load-bearing** — nothing enforces it; it is a self-reminder injected back into the prompt after compaction (P2) so the agent does not lose its own to-do list across a context-window transform. This task lands two pieces: (1) a `plan` channel on `RuntimeState` with a reducer, and (2) the `plan_write` tool that writes into it. Injection (P2) and the read API (P3) are separate tasks.

## Task-Specific Shared Contract

**Plan item shape** (the unit stored in the channel):
- `id: string` — stable identifier for the item, supplied by the agent (or assigned deterministically from list position if the agent omits it — pick one and document; the v1 design favors agent-supplied ids so a re-write can preserve item identity across calls). The id is what P3's API and P4's Console `data-testid="plan-item-{id}"` key on.
- `title: string` — the item text.
- `status: string` — one of `pending` | `in_progress` | `completed`. Any other value is rejected by the tool (see below).

**Write semantics — RESOLVED for v1 (the open design question):** **full-list replace**, matching Claude Code's `TodoWrite` shape — each `plan_write` call carries the *entire* plan and overwrites the channel verbatim. **Not** patch/delta ops. Document this choice in the reducer docstring and the tool docstring, citing the design's "What stays open … Write semantics: full-list replace (Claude Code's `TodoWrite` shape) vs. patch ops" line as the resolution. Rationale to record: a flat self-reminder has no merge concerns, replace keeps the injected block byte-stable between unchanged writes (cache-friendly, mirrors `_list_replace_reducer`'s rationale at `state.py:70`), and it sidesteps patch-conflict semantics the plan does not need.

**Reducer:** a replace reducer (`return b`) annotating the `plan` field — same shape as `_list_replace_reducer`. A node that does not return `plan` leaves the prior value intact (so the plan persists across turns that do not call `plan_write`); a node that does return it replaces wholesale. Follow the `state.py` design-note discipline: no `Optional[T]`, reducer-safe sentinel default `[]`.

**Tool — `plan_write`:**
- Built-in, registered in `_get_tools` behind an `if "plan_write" in allowed_tools:` gate (allowlist-gated, exactly like the other built-ins). Agents without `plan_write` in their allowlist never see it.
- Input: the full plan as a list of items (`id`, `title`, `status`).
- Effect: writes the plan **verbatim** into `state["plan"]` (returns a graph-state update so the reducer replaces the channel). Does NOT transform, re-order, or normalize item content beyond validation.
- Returns a short confirmation `ToolMessage` to the agent (e.g. item count) — enough for the LLM to know the write landed, not the whole plan echoed back.
- **Validation (tool-layer, hard):** reject an item whose `status` is not in the enum; reject when caps are exceeded (below). These are *structural* rejections (the tool returns an error result the LLM can correct), not silent truncation.
- **Does NOT enforce exactly-one-`in_progress`.** That rule is **prompt-layer guidance** delivered in P2's injected preamble, per the design ("Exactly-one-`in_progress` rule: Prompt-layer guidance in the preamble, not tool-layer rejection"). The tool accepts zero, one, or many `in_progress` items without complaint.

**Size caps — RESOLVED for v1 (the open design question "Plan size limits"):** pick and document concrete caps. Proposed v1 values (adjust only with a recorded reason): **max 50 items**, **title max 200 characters per item**. These bound the injected token budget (P2) and the checkpoint payload. Exceeding either cap is a tool-layer rejection with a message naming the cap. Record these as the resolution of the design's "Plan size limits (item count, content length, injection token budget)" open item. (The injection *token* budget is P2's concern; P1 owns item-count + content-length.)

## Affected Component

- **Service/Module:** Worker Service — graph state + built-in tools
- **File paths:**
  - `services/worker-service/executor/compaction/state.py` (modify — add `plan` field + replace reducer)
  - `services/worker-service/tools/plan_tools.py` (new — `plan_write` tool definition + plan-item validation)
  - `services/worker-service/executor/graph.py` (modify — register `plan_write` in `_get_tools` behind the allowlist gate)
  - `services/worker-service/tests/test_plan_tools.py` (new — reducer + tool unit tests)
- **Change type:** new `RuntimeState` field + reducer; new tool module; tool registration

## Dependencies

- **Must complete first:** None. P1 is the root of the Planning Primitive track.
- **Provides output to:** **P2** (reads `state["plan"]` to inject post-compaction), **P3** (projects the `plan` channel from the latest checkpoint), **P4/P5** (downstream of P3).
- **Shared interfaces/contracts:** the plan-item shape (`{id, title, status}`), the `plan` channel name on `RuntimeState`, and the replace-write contract — all three are load-bearing for P2/P3.
- **Worktree note:** shared `state.py` + `_get_tools` with the Supervisor Topology track — see SHARED-FILE warning above.

## Implementation Specification

### Modify: `RuntimeState` (state.py)
- Add a module-level `_plan_replace_reducer(a, b) -> list` (`return b`) with a docstring documenting the full-list-replace decision and the cache-stability rationale (cross-reference `_list_replace_reducer`).
- Add `plan: Annotated[list[dict], _plan_replace_reducer]` to `RuntimeState` (default `[]`, no `Optional`). Items are plain dicts (`{id, title, status}`) so they serialize into the checkpoint JSONB without custom codecs.

### New: `tools/plan_tools.py`
- A `plan_write` coroutine + a `StructuredTool` factory (mirror `tools/definitions.py` conventions). Input schema is the list of items; validate enum + caps; return a graph-state update writing `plan` verbatim plus a short confirmation message.
- A small validator (`validate_plan_items`) reused by the tool and importable by tests.

### Modify: `_get_tools` (graph.py)
- Add an `if "plan_write" in allowed_tools:` block that appends the `plan_write` `StructuredTool`, mirroring the existing built-in registration blocks. No change to `MAX_TOOLS_PER_AGENT` accounting beyond adding one tool.

## Acceptance Criteria

- [ ] `RuntimeState` has a `plan` field with a replace reducer; default is `[]`; a node returning `{"plan": [...]}` replaces the channel, a node omitting `plan` leaves it intact.
- [ ] `plan_write` is registered in `_get_tools` **only** when `"plan_write"` is in `allowed_tools`; absent → tool not exposed.
- [ ] Calling `plan_write` with a valid full list writes it verbatim into `state["plan"]` (round-trips unchanged) and returns a short confirmation message.
- [ ] `plan_write` with an item whose `status` is not in `{pending, in_progress, completed}` is rejected with a message naming the allowed values.
- [ ] `plan_write` with two `in_progress` items **succeeds** (one-`in_progress` is prompt-layer, not tool-layer).
- [ ] `plan_write` with 51 items is rejected naming the 50-item cap; 50 items succeeds.
- [ ] `plan_write` with a title exceeding 200 chars is rejected naming the cap.
- [ ] The reducer/tool/validator unit tests pass under the pinned venv with no infra.

## Testing Requirements

- **Unit (no infra):** reducer replace-vs-preserve behavior; `plan_write` happy path (verbatim write); each rejection case (bad status, over item cap, over title cap); the explicit "two `in_progress` accepted" case (guards against accidentally enforcing the prompt-layer rule at the tool layer).
- **Registration:** assert `plan_write` appears in the `_get_tools` output iff allowlisted (a fake/minimal allowlist suffices — no DB).
- Run the narrowest scope only.

## Constraints and Guardrails

- **Full-list replace only** — do not implement patch/delta write ops in v1.
- **Tool must NOT enforce one-`in_progress`** — that is P2's prompt-layer preamble.
- Do not normalize, re-order, or rewrite item content — write verbatim (preserving byte-stability for P2's cache-adjacent injection).
- Do not inject the plan into the prompt here — injection is P2.
- Do not add a mutation API or any HTTP surface — the read-only `GET` is P3; there is no plan PATCH anywhere (design decision).
- Do not change `state.py`'s existing fields/reducers; only add the `plan` channel.
- Worktree-isolate parallel edits to `state.py` / `_get_tools` against the Supervisor Topology track.

## Assumptions

- Allowlist plumbing (`allowed_tools` reaching `_get_tools`) exists and matches how other built-ins are gated.
- Checkpoint JSONB serializes a `list[dict]` of primitives without a custom codec (consistent with existing dict-valued state fields).
- `langchain_dumps`/checkpoint persistence treats the `plan` channel like any other `RuntimeState` field — no special-casing needed.

<!-- AGENT_TASK_END: task-p1-plan-state-and-tool.md -->
