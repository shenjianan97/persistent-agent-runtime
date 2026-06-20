<!-- AGENT_TASK_START: task-s6-supervisor-fanout-iteration.md -->

# Task S6 — Supervisor Node + Structural `Send` Fan-out + Iteration Loop

## Agent Instructions

You are a software engineer implementing one module of a larger system. Your scope is strictly limited to this task.

**CRITICAL PRE-WORK:** Before beginning implementation, you MUST read:
1. `docs/design-docs/agent-modes/design.md` — sections "Topology 2: Supervisor" (the Supervisor box + iteration loop), "What the Supervisor topology owns" (the structured **subtask-emission contract** — *"the Supervisor's output is parsed into `subtasks: [...]`, not freeform text"* — and "Per-iteration parallel fan-out: the graph reads the Supervisor's subtask list and `Send`s N sub-agents in parallel"), "Subagent count — dynamic, capped", "Partial subagent failure" (re-dispatch vs. proceed vs. fail-only-if-zero; the stable `subtask` vs. `iteration` markers), "Execution model: in-process fan-out (Pattern A)", "Shared fan-out machinery", "Durability" (the `subagent_results` **checkpointed reducer keyed by `subtask`**; resume-forward vs. rollback), and the "Open decisions" section (the **checkpoint payload size of a wide fan-out** item — S6 owns the v1 log-bytes mitigation).
2. `docs/exec-plans/active/agent-modes/supervisor-topology/plan.md` — §A0 (invariant 1 *Pattern A*, invariant 5 *budget defers to Track 3*, invariant 6 *`subagent_results` reducer keyed by `subtask`, idempotent*, invariant 7 *depth cap 2*), §A4.1 (**S6** row — the load-bearing contract), §A5 (Supervisor `Send` → `run_subagent`; `subagent_results` reducer; partial-failure), §A7 (the `supervisor_iteration` event payload), and §A8 (the wide-fan-out checkpoint-size open item, and the "Implementer rebuilds Pattern B" review gate).
3. `services/worker-service/executor/graph.py` — the existing ReAct graph build (`_build_graph` `:1059`, `StateGraph(state_type)` `:1555`, conditional edges, `astream(... durability="sync")` `:3127`) as the **pattern** for graph wiring. LangGraph 1.0.5 supports `Send` and subgraphs but they are **currently UNUSED in this repo** — S6 introduces structural `Send`.
4. `task-s5-supervisor-scope.md` (S5) — `supervisor/state.py` superset and `scope_node`. S6 reads `brief`, increments `iteration`, writes `subtasks`, and owns the `subagent_results` reducer that S5 declared.
5. `task-s3-shared-fanout-helper.md` (S3) — `executor/subagents/fanout.py::run_subagent(prompt, tools, *, ceiling, depth, ...) -> SubagentResult`, including its failure-marker shape and the `MAX_SUBAGENT_DEPTH=2` rejection. S6 `Send`s subtasks **through** `run_subagent`; it does not reimplement the helper.
6. `task-s9-observability-events.md` (S9) — S9 **owns** the `task_events` event-type migration and the `_insert_task_event` plumbing. S6 only **calls** the emit helper for `supervisor_iteration`; if S9 has not landed the new event type yet, S6 calls the helper behind the same interface and S9 wires the migration (deploy-order constraint, §A6).

**CRITICAL POST-WORK:** After completing this task:
1. Run the narrowest worker tests through the pinned venv / isolated harness: `make e2e-test PYTEST_ARGS='-k supervisor_fanout or subagent_results or iteration'`. For unit-level node/reducer tests: `services/worker-service/.venv/bin/python -m pytest services/worker-service/tests/test_supervisor_fanout.py`. Fix regressions (including pre-existing failures surfaced).
2. Update `docs/exec-plans/active/agent-modes/supervisor-topology/progress.md` — mark S6 "Done".

## Context

The Supervisor node is the brain of the Deep Research graph. Each iteration it reads the immutable `brief` (and the findings so far) and **decides how to decompose the work into subtasks**, emitting a *parsed* `subtasks: [{subtask, prompt}]` list — not freeform prose. The graph then `Send`s those subtasks in parallel through S3's `run_subagent`, gathers their results into the `subagent_results` reducer, and returns control to the Supervisor, which decides "need more research?" — looping up to `max_iterations`, with at most `max_fanout_per_iteration` subtasks per round.

The count is **dynamic within caps**: the design cites Anthropic's early Lead Researchers *"spawning 50 subagents for simple queries"* before tuning — the LLM picks the count, the caps bound the cost envelope. Partial failures do **not** sink the run: the Supervisor receives successes plus a failure marker per non-returning subagent and decides re-dispatch / proceed; the graph fails **only if zero subagents returned**.

Because re-dispatch happens across rounds, results are keyed by a stable logical `subtask` id (distinct from the `iteration` round marker). The `subagent_results` reducer is checkpointed and keyed by `subtask` so a crash resume-forward restores completed siblings and a re-dispatched subtask updates its own entry rather than duplicating (§A0 invariant 6).

## Task-Specific Shared Contract

- **`supervisor/nodes.py::supervisor_node`** emits a **parsed** contract: `subtasks: [{subtask, prompt}]`.
  - `subtask` is the **stable logical id** (string) — the same id is reused across rounds when a failed subtask is re-dispatched, so the Console tree links "subtask X: failed (round 1) → succeeded (round 2)" (§"Partial subagent failure", §"Observability").
  - **S6 MINTS the `subtask` id deterministically — the id is NEVER trusted from the LLM's emission (§A11-E8).** Newly-emitted subtasks get `f"{iteration}.{index}"` (index = position within the round's emitted list). The prior id is carried forward **only** on explicit re-dispatch of a previously-failed subtask. Rationale: the `subagent_results` reducer is keyed by `subtask`; it is idempotent *across* rounds, but nothing guarantees uniqueness *within* one round — if the LLM emitted two subtasks with a colliding id, the reducer would silently overwrite one entry → lost findings + burned tokens. Deterministic minting makes within-round collision structurally impossible.
  - `prompt` is the focused instruction the sub-agent runs.
  - Parsing is structured (the Supervisor LLM's output is parsed into this list — use the repo's structured-output convention, e.g. the same approach S5 used for the clarity assessment; do not accept freeform text as the subtask list).
  - The emission is **clamped to `max_fanout_per_iteration`** (from `agent_config.supervisor`). If the LLM emits more, truncate to the cap and emit a `supervisor_iteration` event recording the cap reason (no silent truncation — §A7).
- **Structural fan-out via `Send`:** the graph reads `subtasks` and `Send`s each through `run_subagent` (S3), in parallel, in-process (Pattern A — one `thread_id`, one task row, one checkpoint stream). Each `Send` passes the per-sub-agent `ceiling` (the Supervisor default from `agent_config.supervisor`) and the current `depth` (cap 2 enforced inside `run_subagent`). This is the **structural** driver of the shared helper — the deterministic counterpart to S4's LLM-emergent `dispatch_subagent`.
- **`subagent_results` — checkpointed reducer keyed by `subtask`:**
  - Implement the reducer (the channel S5 declared). Merge semantics: an incoming `{subtask -> result_or_failure_marker}` **updates its own keyed entry idempotently** — re-dispatching the same `subtask` overwrites its prior marker, never appends a duplicate.
  - Both **successful findings and failure markers** survive a crash (they are checkpoint state). On crash resume-forward, completed siblings are restored (not recomputed); only unfinished branches re-run (LangGraph `pending_writes` semantics — §"Durability").
- **Iteration loop:** bounded by `max_iterations` (from `agent_config.supervisor`). After each fan-out gather, `supervisor_node` decides `continue | stop`:
  - `continue` → next round (increment `iteration`, emit more subtasks, possibly re-dispatching failed `subtask`s under their existing ids).
  - `stop` → route to the Writer (S7).
  - Hitting `max_iterations` forces `stop` and emits a `supervisor_iteration` event with the cap reason.
- **Partial-failure decision (in-graph, never a dead-lettered task):** the node receives the partial result set (successes + per-subtask failure markers). It may re-dispatch, proceed, or — **only when zero subagents returned in a round AND it cannot make progress** — fail the graph. Fail-fast is the all-failed case alone; one flaky web fetch must not sink the run (§"Partial subagent failure").
- **Events (S9 owns the type; S6 calls the helper):** emit `supervisor_iteration` with `{iteration, subtasks_emitted, decision: continue|stop, reason}` (§A7). The per-sub-agent `subagent_started/finding/failed` events are emitted by S3's helper / S7; S6 emits the iteration-level event only.
- **Open item (S6 owns the v1 mitigation):** a wide fan-out writes one large checkpoint per super-step holding all sub-agents' accumulated state. v1: when the serialized `subagent_results` / fan-out payload exceeds a threshold, **`log()` the payload byte size** (a `checkpoint.oversized`-style warning) so operators see the pressure. The actual **cap/offload is deferred** (§A8, §"Open decisions"). Do not cap or truncate state in v1 — only log.

## Affected Component

- **Service/Module:** Worker Service — Supervisor topology (Supervisor node, fan-out wiring, iteration, reducer)
- **File paths:**
  - `services/worker-service/executor/supervisor/nodes.py` (modify — add `supervisor_node`; S5 added `scope_node`)
  - `services/worker-service/executor/supervisor/state.py` (modify — implement the `subagent_results` reducer S5 declared)
  - `services/worker-service/executor/supervisor/graph.py` (new or modify — the `Send` fan-out edge + the iteration conditional edge; S8 owns final compile/branching but S6 supplies the Supervisor↔fan-out↔Supervisor sub-wiring)
  - `services/worker-service/executor/supervisor/prompts.py` (modify — add the Supervisor prompt template with the iteration-decision protocol + structured subtask-emission contract; additive with S5/S7)
  - `services/worker-service/tests/test_supervisor_fanout.py` (new)
- **Change type:** new node + reducer implementation + structural `Send` wiring + iteration loop

## Dependencies

- **Must complete first:** **S3** (the helper to `Send` through) and **S5** (the state superset + `scope_node` + the declared `subagent_results` channel). S5 → S6 → S7 **serialize** (shared `nodes.py`/`state.py`); if run in parallel with any agent touching those files, use `isolation: "worktree"`.
- **Provides output to:** **S7** (the Writer consumes the accumulated `findings`/`subagent_results`; the iteration `stop` routes to the Writer), **S8** (compiles the full graph and branches `_build_graph` on topology; S8's wide-fan-out cost test depends on S6's fan-out wiring), **S9** (the `supervisor_iteration` event S6 emits).
- **Shared interfaces/contracts:** the `subtasks: [{subtask, prompt}]` parsed contract; the `subagent_results` reducer (keyed by `subtask`, idempotent); the `supervisor_iteration` event payload.

## Implementation Specification

### `supervisor_node`

Async node. Reads `brief`, `iteration`, and the current `subagent_results`/`findings`. Calls the Supervisor LLM with the Supervisor prompt template (iteration-decision protocol + subtask-emission contract). Parses the structured output into `subtasks: [{subtask, prompt}]`, clamps to `max_fanout_per_iteration`, and sets the loop decision. **Mints each newly-emitted subtask's `subtask` id deterministically as `f"{iteration}.{index}"` — never reading the id from the LLM's emission (§A11-E8).** On a re-dispatch round, carry forward the prior `subtask` id for the retried subtask **only** (so the reducer updates-in-place); all other (new) subtasks get freshly-minted round-keyed ids.

### Structural `Send` fan-out

Wire the fan-out as a LangGraph `Send` map over `subtasks` into a node that invokes `run_subagent` (S3) per subtask, passing `ceiling`, `depth`, and the source/tool allowlist from `agent_config.supervisor`. The Supervisor `Send` passes **`depth=1`** for the first fan-out level — the Supervisor itself is depth 0 — so the depth-2 cap enforced in `run_subagent` (S3) is not ambiguously interpreted as depth-3 (§A11-E8 wording note; §A4.1 S6 row). Gather results into `subagent_results` via the keyed reducer. This is the repo's **first** use of `Send` — follow the LangGraph 1.0.5 `Send` API; mirror `graph.py` conventions for node/edge registration.

### `subagent_results` reducer (in `state.py`)

Implement the merge: input is a partial `{subtask -> SubagentResult|FailureMarker}`; merge updates the keyed entry idempotently. Unit-test idempotency directly: applying the same subtask result twice yields one entry; re-dispatch of a failed subtask overwrites its failure marker with the success.

### Iteration conditional edge

A conditional edge after the fan-out gather routes back to `supervisor_node` (continue) or to the Writer (stop / `max_iterations` reached). Emit `supervisor_iteration` on each decision and on each cap hit.

### Partial-failure handling

Compute, per round, the success/failure split from `subagent_results`. Pass it to `supervisor_node`'s decision. Fail the graph only when zero subagents returned and no progress is possible; otherwise proceed/re-dispatch.

### Wide-fan-out payload logging

After the gather, if the serialized fan-out payload exceeds a configured byte threshold, `log()` the byte size with a clear marker. Do not truncate. Document the deferred cap with a comment linking to §"Open decisions".

## Acceptance Criteria

- [ ] `supervisor_node` emits a **parsed** `subtasks: [{subtask, prompt}]` list (structured, not freeform), clamped to `max_fanout_per_iteration`.
- [ ] The graph `Send`s subtasks in parallel through `run_subagent`, passing `ceiling` + `depth`; results merge into `subagent_results`.
- [ ] `subagent_results` is keyed by `subtask` and **idempotent**: applying the same subtask result twice yields a single entry; a re-dispatched failed subtask overwrites its marker (no duplicate).
- [ ] **Within-iteration id minting (§A11-E8):** `subtask` ids are minted by S6 as `f"{iteration}.{index}"` and never read from the LLM's emission; carry-forward applies only on explicit re-dispatch. Two **distinct** subtasks emitted in the **same round** get **distinct** ids and **both** results survive in `subagent_results` — verified by a within-iteration collision test that is distinct from the cross-round idempotency test (the LLM emitting a duplicate/colliding id must not lose a result).
- [ ] Crash resume-forward restores completed siblings (not recomputed) and only re-runs unfinished branches — verified with a checkpoint-resume test.
- [ ] The iteration loop is bounded by `max_iterations`; hitting the cap forces `stop` and emits `supervisor_iteration` with the cap reason.
- [ ] Partial failure: with ≥1 success the run **proceeds** (Supervisor decides re-dispatch/proceed); with **zero** returns and no progress the graph fails — verified by separate tests.
- [ ] `supervisor_iteration` events carry `{iteration, subtasks_emitted, decision, reason}` (emitted via the S9-owned helper interface).
- [ ] A wide-fan-out payload over the threshold logs its byte size; state is NOT truncated.
- [ ] No Pattern-B artifact introduced (grep your diff for `parent_task_id`, `sub_agent_id`, `waiting_for_subagent` → none).
- [ ] Narrowest worker tests pass via the isolated harness; `progress.md` marks S6 Done.

## Testing Requirements

- **Unit tests** (fake model + fake `run_subagent` returning canned successes/failures): subtask parsing + clamp to `max_fanout_per_iteration`; **deterministic id minting + within-iteration collision (§A11-E8)** — a fake model emitting two distinct subtasks with a colliding (or absent) LLM-chosen id yields two distinct `f"{iteration}.{index}"` ids and both results survive in `subagent_results` (distinct from the cross-round idempotency test); `Send` fan-out dispatches N subagents; reducer idempotency + re-dispatch overwrite (carry-forward id); iteration bound + cap-reason event; partial-failure proceed vs. zero-return fail; `supervisor_iteration` payload shape; oversized-payload log fires past threshold and not below.
- **Crash/resume test:** drive the graph to a mid-fan-out checkpoint, simulate restart, assert completed siblings are restored and only unfinished branches re-run (resume-forward, not recompute).
- **Worktree-concurrency-safe:** ephemeral ports for anything binding a socket (`scripts/e2e/free-port.py` / `:0`).
- **Run narrowest scope:** `make e2e-test PYTEST_ARGS='-k supervisor_fanout'`; direct `pytest` for the unit/reducer layer. Do not run full `make test` unless the change reaches beyond `executor/supervisor/`.

## Constraints and Guardrails

- **NO Pattern B.** No sub-agent task rows, no `parent_task_id` column/tree, no per-sub-agent leases, no `sub_agent_id` cost-ledger column, no `waiting_for_subagent` pause state. Fan-out is in-graph via `Send` (§A0 invariant 1; review gate §A8).
- **NO `agent_config.mode` field.** Caps come from `agent_config.supervisor` (validated by S1); S6 reads the loaded snapshot.
- **Findings quotes are immutable** (§A0 invariant 4). The reducer may select/reorder/key — it must **never** mutate a finding's `supporting_quote`.
- **`subagent_results` is keyed by `subtask` and idempotent** (§A0 invariant 6) — re-dispatch updates in place, never duplicates.
- **Depth cap 2** (§A0 invariant 7) — the `Send` passes the current `depth`; rejection lives in `run_subagent` (S3). Do not bypass it.
- **Do not switch durability to async.** Any test graph invokes with `durability="sync"` (matching `graph.py:3127`); S8 owns the production compile but must not be undermined by an async test harness here.
- **Budget defers to Track 3** (§A0 invariant 5) — S6 adds NO ledger rows, NO per-sub-agent budget rollup, NO refund path. Cost rolls into the parent (S8 audits this).
- **Budget pause at the fan-out super-step boundary, not mid-`Send`-branch (§A11-E2; S8 owns the cost-loop change).** A mid-super-step pause `return` exits the astream loop while sibling sub-agent branches are still live, abandoning them with possibly-partial `pending_writes` (and re-billing completed siblings on resume). S6's iteration loop must therefore checkpoint/evaluate **between iterations** — at fan-out super-step completion — and never abandon an in-flight fan-out to pause. The actual pause-evaluation point lives in S8's cost loop; S6 must not structure the iteration loop in a way that forces a mid-super-step exit.
- Do not own the `task_events` migration or `ActivityProjectionService` mapping — that is S9. S6 only calls the emit helper.
- Wide-fan-out: **log only** in v1. Do not cap/offload checkpoint payload (deferred — §"Open decisions").

## Assumptions

- S3's `run_subagent` returns a structured success or a failure marker (never raises into the graph for ceiling/timeout — it returns a marker), so partial-failure handling reads markers from `subagent_results`.
- S5 has declared the `subagent_results` channel and the `brief`/`iteration`/`subtasks`/`findings` channels; S6 implements the reducer.
- `agent_config.supervisor.{max_fanout_per_iteration, max_iterations, source_allowlist, writer_style}` are present and bounds-validated by S1, reachable from the loaded snapshot.
- LangGraph 1.0.5's `Send` map-reduce works with the Postgres checkpointer under `durability="sync"` (design verified `Send` against `langgraph==1.0.5`).
- The `supervisor_iteration` event helper interface is stable even if S9's migration lands later (deploy-order: migration before emitting code reaches prod — §A6).

<!-- AGENT_TASK_END: task-s6-supervisor-fanout-iteration.md -->
