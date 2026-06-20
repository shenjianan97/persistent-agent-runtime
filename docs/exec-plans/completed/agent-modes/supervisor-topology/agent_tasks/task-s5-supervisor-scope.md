<!-- AGENT_TASK_START: task-s5-supervisor-scope.md -->

# Task S5 — Supervisor Graph: Scope Node + Conditional Clarify + Immutable Brief

## Agent Instructions

You are a software engineer implementing one module of a larger system. Your scope is strictly limited to this task.

**CRITICAL PRE-WORK:** Before beginning implementation, you MUST read:
1. `docs/design-docs/agent-modes/design.md` — sections "Topology 2: Supervisor" (the four-phase graph and the Scope box), "Pattern provenance" (the Scope row — why the brief is the *"north star"* and why clarity is assessed internally, only asking the user when needed), "What the Supervisor topology owns" (Scope prompt template ownership), "What customers configure" (the `scope_clarification_enabled` knob), and the "Open decisions" section.
2. `docs/exec-plans/active/agent-modes/supervisor-topology/plan.md` — §A0 (especially invariant 1 *Pattern A in-process fan-out only*, invariant 2 *topology immutable*, and the `durability="sync"` rule in §A9), §A4.1 (the **S5** row — `scope_node` produces the immutable `brief` and reuses `waiting_for_input`), §A5 (the Scope clarification integration row), and §A8 (the "Scope clarification deadlocks headless customers" risk → `scope_clarification_enabled=false` proceeds on best-effort).
3. `services/worker-service/executor/graph.py` — the existing ReAct graph build (`_build_graph` `:1059`, `StateGraph(state_type)` `:1555`, `astream(... durability="sync")` `:3127`) as the **pattern** for how this repo constructs and compiles a LangGraph. You are NOT editing this file in S5; you are mirroring its conventions in the new `executor/supervisor/` package.
4. `services/worker-service/executor/compaction/state.py` — the existing `RuntimeState` TypedDict + its reducers. `supervisor/state.py` is a **superset** of this shape (it adds supervisor channels); read it to mirror reducer/annotation conventions.
5. The existing human-in-the-loop pause path: the `interrupt()` tool at `services/worker-service/tools/definitions.py:449`, the `waiting_for_input` state it drives, and resume via `Command(resume=...)`. S5 reuses this exact mechanism — it does NOT invent a new pause state.
6. The S3 handoff contract (`task-s3-shared-fanout-helper.md`, §A4.1 row S3) so `supervisor/state.py` declares the channels S6/S7 will populate (`subagent_results`, `iteration`, `subtasks`, `findings`). S5 defines the state superset; S5 itself only writes `brief`.

**CRITICAL POST-WORK:** After completing this task:
1. Run the narrowest worker tests that cover this change through the pinned venv / isolated harness: `make e2e-test PYTEST_ARGS='-k scope or supervisor_state'` (never raw `pytest tests/backend-integration` in a worktree — §A9). Unit-level node tests run via `services/worker-service/.venv/bin/python -m pytest services/worker-service/tests/<your_test_file>.py`. Fix any regressions, including pre-existing failures your change surfaces.
2. Update `docs/exec-plans/active/agent-modes/supervisor-topology/progress.md` — mark S5 "Done" (create the file if S3/S4 have not, mirroring an existing track's `progress.md` shape).

## Context

The Supervisor topology (customer-facing "Deep Research") is a fixed four-phase graph: **Scope → Supervisor (with iteration) → parallel Subagents → Writer**. S5 builds the first phase and the **state superset** the rest of the graph reads/writes.

Scope does two things: (1) assess whether the research query is clear enough to act on, and — *only when configured to* — ask the user a clarifying question, pausing the run; and (2) produce the **brief**, an immutable "north star" goal anchor that every downstream node (Supervisor, Subagents, Writer) refers back to. The design's Pattern-provenance table is explicit that *"users rarely provide sufficient context in a research request"* and that the brief *"serves as our north star for success."* Our refinement over LangChain's Open Deep Research: Scope evaluates clarity **internally** and asks the user only when it judges the query ambiguous — and never asks at all when `supervisor.scope_clarification_enabled=false` (headless customers).

S5 is mostly graph-node logic. The clarity assessment and brief generation are LLM calls; the clarification pause **reuses** the existing `waiting_for_input` machinery rather than introducing anything new.

## Task-Specific Shared Contract

- **New package `executor/supervisor/`** with `state.py` (this task) and `nodes.py::scope_node` (this task). `graph.py`, `prompts.py`, `citations.py` are stubbed/owned by S6/S7/S8 — S5 may create `prompts.py` with only the Scope template if convenient, but the Supervisor/Subagent/Writer templates are S7's (§A4.1, S7 owns `prompts.py`). Coordinate by leaving `prompts.py` additive.
- **`supervisor/state.py` — the superset state**, a TypedDict that is a strict superset of `RuntimeState` (so the Supervisor graph can run the same compaction/checkpoint machinery) plus these supervisor channels:
  - `brief: str` — the immutable north star. **Write-once**: set by `scope_node`, never mutated by any later node. (Immutability is a contract, not a runtime lock — document it; downstream tasks must not write it.)
  - `iteration: int` — the round counter (S6 increments; S5 initialises to 0).
  - `subtasks: list[dict]` — the Supervisor's parsed `[{subtask, prompt}]` emission (S6 populates; S5 initialises empty).
  - `subagent_results: <reducer channel keyed by subtask>` — the checkpointed reducer (S6 owns the reducer; S5 declares the channel + its annotation so the graph type-checks). Keyed by `subtask`, idempotent (§A0 invariant 6).
  - `findings: list[dict]` — accumulated `{finding_id, claim, source_url, supporting_quote}` (S7 populates; S5 declares the channel).
  - `clarification_question: str | None` and any field needed to thread the clarify Q&A through `waiting_for_input` / `Command(resume=...)` — reuse whatever the existing `waiting_for_input` path already threads; do not add a parallel channel if one exists.
- **`scope_node` contract:**
  1. Reads the task input (the research query) from state.
  2. LLM clarity assessment using the Scope prompt template (Scope owns its template — §"What the Supervisor topology owns"). Output is a structured judgement: clear-enough vs. needs-clarification, plus (if needs-clarification) a single clarifying question.
  3. **Conditional clarify gate, honoring `supervisor.scope_clarification_enabled`:**
     - When the flag is **true** AND the assessment says ambiguous → call `interrupt()` (reusing the `waiting_for_input` state) to surface the question; on resume via `Command(resume=...)`, fold the user's answer into the brief generation.
     - When the flag is **false** (headless) → **never** `interrupt()`. Proceed on a best-effort brief synthesised from the original query alone (§A8 risk row). This must hold even if the assessment judged the query ambiguous.
  4. Generates the **brief** (LLM) and writes it once into `state["brief"]`. Initialises `iteration=0`, `subtasks=[]`, and leaves the reducer channels at their empty defaults.
- The flag is read from the agent config snapshot (`agent_config.supervisor.scope_clarification_enabled`), which `execute_task` already loads (`:2615`). S5 reads it from state/config the same way other nodes read agent config; it does NOT re-fetch from the API.

## Affected Component

- **Service/Module:** Worker Service — Supervisor topology (graph nodes + state)
- **File paths:**
  - `services/worker-service/executor/supervisor/__init__.py` (new)
  - `services/worker-service/executor/supervisor/state.py` (new — the superset TypedDict + channel annotations)
  - `services/worker-service/executor/supervisor/nodes.py` (new — `scope_node` only in S5; S6/S7 append `supervisor_node`, `subagent_node`, `writer_node`)
  - `services/worker-service/executor/supervisor/prompts.py` (new — Scope template only in S5; additive; S7 owns the rest)
  - `services/worker-service/tests/test_supervisor_scope.py` (new)
- **Change type:** new package + new node + new state superset

## Dependencies

- **Must complete first:** **S3** (the shared fan-out helper exists and its `SubagentResult` / channel shape is settled — `state.py` declares the channels S6 drives through `run_subagent`). S5 does not call `run_subagent`, but its state declarations must be compatible with S3's output.
- **Provides output to:** **S6** (reads `brief`, `iteration`, writes `subtasks`/`subagent_results`), **S7** (reads `findings`/`brief`), **S8** (compiles the graph that wires `scope_node` first). S5/S6/S7 **build on each other and must serialize** (S5 → S6 → S7) or be worktree-isolated — they all add to `supervisor/nodes.py` and `supervisor/state.py`.
- **Shared interfaces/contracts:** `supervisor/state.py` superset shape; the `scope_node` signature; the reuse of `waiting_for_input` + `Command(resume=...)`.

## Implementation Specification

### `supervisor/state.py`

Define the superset `TypedDict` (extend/compose `RuntimeState` from `executor/compaction/state.py` — do not re-declare its fields by copy if a `total=False` composition or inheritance is cleaner; mirror that file's reducer-annotation style). Add the supervisor channels listed in the Shared Contract. For the `subagent_results` reducer channel, declare the annotation now (S6 implements the merge function keyed by `subtask`); a placeholder identity/append annotation that S6 replaces is acceptable **only if** S6 is the very next serialized task — otherwise declare the keyed-merge contract S6 will fill. Document each channel's owner (which node writes it) in a comment.

### `supervisor/nodes.py::scope_node`

Implement the node per the contract above. The node is `async` (matching the repo's async node convention — see the ReAct nodes in `graph.py`). Use the existing model/LLM access path the ReAct nodes use; do not introduce a new provider client. The clarify interrupt MUST go through the same `interrupt()` / `waiting_for_input` / `Command(resume=...)` path as `tools/definitions.py:449` — read that path and reuse it; do not add a `waiting_for_input`-parallel state.

### `supervisor/prompts.py`

Add only the **Scope** prompt template (clarity assessment + clarification-question generation + brief generation). Keep it a module-level template/string-builder consistent with how the repo holds prompts. Leave the module additive for S7's Supervisor/Subagent/Writer templates.

### Honoring `scope_clarification_enabled`

The flag short-circuits the clarify branch. When false, `scope_node` must not call `interrupt()` under any assessment outcome — verify this with a test that sets the flag false on an ambiguous query and asserts no interrupt is raised and a best-effort brief is produced.

## Acceptance Criteria

- [ ] `executor/supervisor/state.py` defines a TypedDict that is a strict superset of `RuntimeState`, adding `brief`, `iteration`, `subtasks`, `subagent_results` (keyed-by-`subtask` reducer channel), and `findings`, each annotated and owner-commented.
- [ ] `scope_node` produces a non-empty `brief` and writes it exactly once; it initialises `iteration=0` and `subtasks=[]`.
- [ ] With `scope_clarification_enabled=true` and an **ambiguous** query, `scope_node` calls `interrupt()` (reusing `waiting_for_input`), and on `Command(resume="<answer>")` folds the answer into the brief — verified with a fake model + a resume.
- [ ] With `scope_clarification_enabled=true` and a **clear** query, `scope_node` does NOT interrupt and produces a brief directly.
- [ ] With `scope_clarification_enabled=false`, `scope_node` NEVER interrupts (even on an ambiguous query) and produces a best-effort brief from the query alone.
- [ ] The brief, once written, is not mutated by `scope_node` on any path (write-once).
- [ ] No new pause state is introduced — the clarify pause is the existing `waiting_for_input`.
- [ ] Narrowest worker tests pass via the pinned venv / isolated harness; `progress.md` updated to mark S5 Done.

## Testing Requirements

- **Unit tests** (fake/stub model, no live LLM): clear-query → brief, no interrupt; ambiguous + flag-true → interrupt then resume → brief incorporates the answer; ambiguous + flag-false → no interrupt, best-effort brief; brief write-once on every path; state superset includes all declared channels with correct annotations.
- **Worktree-concurrency-safe:** if any test binds a port or spawns a server, use an ephemeral port (`scripts/e2e/free-port.py` / `:0`) — though node-level unit tests with a fake model should need neither.
- **Run narrowest scope:** `make e2e-test PYTEST_ARGS='-k scope'` for harness coverage; direct `pytest` on the new test file for the unit layer. Do not run the full `make test` suite unless your change reaches beyond `executor/supervisor/`.

## Constraints and Guardrails

- **NO Pattern B.** Do not create sub-agent task rows, a `parent_task_id` column/tree, per-sub-agent leases, a `sub_agent_id` cost-ledger column, or a `waiting_for_subagent` pause state. Scope's clarify pause is the existing `waiting_for_input` only (§A0 invariant 1).
- **NO `agent_config.mode` field.** Topology/preset selection is upstream (S1/S2); S5 reads the already-loaded config snapshot.
- **Findings quotes are immutable** (§A0 invariant 4). S5 only declares the `findings` channel; it must not define any transform that would let a later node mutate `supporting_quote`.
- **Do not switch durability to async.** S5 does not compile the graph (S8 does), but any local test graph you build to exercise `scope_node` must invoke with `durability="sync"`, matching `graph.py:3127`.
- Topology is immutable after creation (§A0 invariant 2) — S5 never writes back to agent config.
- Do not add Console UI (S10) or new `task_events` types (S9).
- Keep `prompts.py` additive so S7 can land the remaining templates without conflict; do not pre-write Supervisor/Subagent/Writer templates here.

## Assumptions

- S3 has landed: `executor/subagents/fanout.py::run_subagent` and its `SubagentResult` shape are available, and `MAX_SUBAGENT_DEPTH = 2` is defined there.
- `execute_task` loads the agent config snapshot (`:2615`) including the `supervisor` sub-object validated by S1; `scope_clarification_enabled` is reachable from node state/config the way other agent-config fields are.
- The `interrupt()` / `waiting_for_input` / `Command(resume=...)` path from `tools/definitions.py:449` is reusable from a graph node, not only from a tool.
- The repo's nodes are `async` and share a model-access helper; S5 follows that convention.

<!-- AGENT_TASK_END: task-s5-supervisor-scope.md -->
