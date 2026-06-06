<!-- AGENT_TASK_START: task-s8-graph-build-branching.md -->

# Task S8 — `_build_graph` Topology Branch + `durability="sync"` + Cost-Attribution Mechanism + Super-Step-Boundary Budget Pause

## Agent Instructions

You are a software engineer implementing one module of a larger system. Your scope is strictly limited to this task.

**CRITICAL PRE-WORK:** Before beginning implementation, you MUST read:
1. `docs/design-docs/agent-modes/design.md` — sections "Two-layer naming and the config model" (topology is the source of truth; immutable; `_build_graph` selects shape from it), "Execution model: in-process fan-out (Pattern A)" (one task / one `thread_id` / one checkpoint; the `durability="sync"` note), "Durability" (resume-forward vs. rollback), "Budget and redrive" (budget **defers wholesale to Track 3**; all sub-agent cost is the **parent task's** cost — no rollup, no per-tree composition, no refund; *"Only provider-unbilled tokens are free"*), and "Shared fan-out machinery" (the per-super-step cost-ledger / `checkpoint_id` framing).
2. `docs/exec-plans/active/agent-modes/supervisor-topology/plan.md` — §A0 (invariant 1 *Pattern A — no `sub_agent_id` column*, invariant 5 *no per-tree rollup, pause-not-fail*, **invariant 9 *cost attribution is a built mechanism, not automatic — the cost loop is hardcoded to `event["agent"]`; Supervisor/sub-agent spend is silently dropped unless S8 extends it***), §A4 ("Cost ledger: unchanged schema, but a **new attribution path** (S8) is required … S8 adds an **additive** `model_token_spend` ledger write at the parent's super-step `checkpoint_id` aggregating every LLM-bearing Supervisor node's `usage_metadata` — Pattern A, no `sub_agent_id` column; the `compaction.tier3` partial-unique index is *not* in play; the per-checkpoint `checkpoints.cost_microdollars` write must be **additive** via `add_cost_and_preserve_metadata`, never overwrite"), §A4.1 (**S8** row — cost-attribution *mechanism*, not audit; super-step-boundary pause), §A5 (`_build_graph` topology switch — unknown topology → fail task build), §A8, and **§A11 rows E1 (Blocker — cost loop is `event["agent"]`-only) and E2 (Blocker — budget pause mid-fan-out abandons live sibling branches)** — the two corrected contracts this task builds against — plus §A11-E7 (task-timeout reachability for long fan-outs), and §A9 (`durability="sync"` for the Supervisor compile).
3. `services/worker-service/executor/graph.py` — the load-bearing anchors you edit:
   - `_build_graph` `:1059` — where you branch on topology (this is the **pattern** for the new branch and the existing ReAct path you must preserve).
   - `StateGraph(state_type)` `:1555` and the ReAct LLM node registered `add_node("agent", ...)` `:1556` — the node key the cost loop is gated on. **Supervisor graph nodes (scope/supervisor/fan-out/writer/verify) emit under OTHER keys, so the existing loop never records their spend.**
   - `astream(... durability="sync")` `:3127` — the durability the Supervisor compile/invoke must match.
   - the **cost-recording loop** `:3166` (`if "agent" in event:`) — the loop S8 **extends** to capture `usage_metadata`/`response_metadata` from every LLM-bearing Supervisor node and the fan-out sub-agents, attributing additively to the parent super-step `checkpoint_id` (this is the E1 mechanism, NOT an audit).
   - the **budget pause** `_check_budget_and_pause` `:3796` and its `return` exit `:3273` — S8 evaluates the pause at the **fan-out super-step boundary**, not mid-`Send` (E2). The carve-out skips (`MEMORY_WRITE_NODE_NAME` / `compaction.tier3`, `:3160-3163`) must NOT be widened for fan-out.
   - `add_cost_and_preserve_metadata` (`core/checkpoint_repository.py:127`) — the **additive** per-checkpoint cost path S8 uses for the parent super-step write (COALESCE-preserves; never overwrite).
   - `execute_task` `:2611`, which already loads `agent_config_snapshot` `:2615` — S8 ensures the supervisor path is **reachable** from here.
4. `task-s3-shared-fanout-helper.md` (S3) through `task-s7-subagents-writer-citations.md` (S7) — the Supervisor graph S8 compiles. S8 wires `executor/supervisor/graph.py` into `_build_graph`; it does not reimplement nodes.
5. `services/worker-service/.../core/cost_ledger_repository.py` (`insert_cost_row`, `sum_task_cost`, `sum_hourly_cost_for_agent`) — to write the wide-fan-out cost test that asserts fan-out spend rolls into the parent's `agent` / `model_token_spend` operation at the parent super-step `checkpoint_id`.

**CRITICAL POST-WORK:** After completing this task:
1. Run the narrowest worker tests through the pinned venv / isolated harness, including the wide-fan-out cost test: `make e2e-test PYTEST_ARGS='-k build_graph or topology or fanout_budget or supervisor_compile'`. Fix regressions (S8 edits `graph.py` heavily — re-run the ReAct graph tests too: `make e2e-test PYTEST_ARGS='-k react or graph'`).
2. Update `docs/exec-plans/active/agent-modes/supervisor-topology/progress.md` — mark S8 "Done".

## Context

S8 is the wiring task: it makes the Supervisor topology **reachable** and **builds the cost-attribution mechanism** that meters it.

`_build_graph` (`:1059`) currently builds the single ReAct graph. S8 branches it on `agent_config.get("topology", "react")` → existing ReAct (absent/`react`) vs. the new `executor/supervisor/graph.py` (`supervisor`). The Supervisor graph is compiled and invoked with **`durability="sync"`**, matching the ReAct path at `:3127` (LangGraph's default is `"async"`; this runtime uses the **stronger** sync mode — do not regress it).

The second half is a **built mechanism, NOT an audit** (§A11-E1, **Blocker**). The cost-recording loop at `:3166` is gated `if "agent" in event:` — and `"agent"` is the *ReAct LLM node name* (registered `:1556`). Supervisor graph nodes (scope/supervisor/fan-out/writer/verify) and the fan-out sub-agents emit under **other** node keys, so the existing loop **never records their LLM spend**: a Deep Research run would record ~$0, and `_check_budget_and_pause` (`:3796`) would never fire — the most expensive topology would run **unmetered**. S8 must **extend** the `:3166` loop to capture `usage_metadata` / `response_metadata` from **every** LLM-bearing Supervisor node (and the fan-out sub-agents) and attribute it **additively** to the **parent's super-step `checkpoint_id`** under the existing `model_token_spend` operation — using the additive checkpoint cost path (`add_cost_and_preserve_metadata`), **never overwrite**. This stays Pattern A: **NO `sub_agent_id` column, NO per-sub-agent rows, NO per-tree rollup, NO refund path**. The `compaction.tier3` partial-unique index is *not* in play (it's scoped to `operation='compaction.tier3'`), so wide fan-out poses no ledger-idempotency hazard. The wide-fan-out test asserts a **NON-ZERO** parent-attributable ledger delta — that is what proves the mechanism works.

Budget pause is evaluated at the **fan-out super-step boundary, not mid-branch** (§A11-E2, **Blocker**). `_check_budget_and_pause`'s pause `return` (`:3273`) exits the astream loop; firing it *mid-`Send`* strands the N−k still-in-flight sub-agent branches with possibly-partial `pending_writes`, and resume re-bills the completed siblings. S8 evaluates the pause at **fan-out super-step completion** — accepting the `max_fanout × ceiling` overshoot the design already budgets — OR proves mid-super-step `pending_writes` coherence before pausing mid-branch.

## Task-Specific Shared Contract

- **`_build_graph` branches on topology.** Read `agent_config.get("topology", "react")`:
  - `"react"` or absent → the **existing** ReAct graph build, **unchanged** behavior. (Existing agents have no `topology` key → `react` → byte-for-byte the current path.)
  - `"supervisor"` → build via `executor/supervisor/graph.py` (the graph S5–S7 assembled: Scope → Supervisor ⇄ fan-out → Writer + verify).
  - any other value → **fail the task build defensively** (API already validates topology at S1, so this is defense-in-depth, not a user-facing path) — §A5.
- **`durability="sync"`** on the Supervisor graph compile/invoke, matching `graph.py:3127`. Do NOT use LangGraph's `"async"` default (§A9).
- **`execute_task` reachability.** `execute_task` (`:2611`) already loads `agent_config_snapshot` (`:2615`). S8 ensures the supervisor branch is reachable from the same task-execution entry — a `topology=supervisor` agent's task runs the Supervisor graph end-to-end (Scope through Writer) under one `thread_id` / one task row / one checkpoint stream (Pattern A).
- **Cost-attribution mechanism (BUILD, don't audit) — §A11-E1:**
  - Extend the `:3166` cost-recording loop (currently gated `if "agent" in event:`) to capture `usage_metadata` / `response_metadata` from **every** LLM-bearing Supervisor node (scope/supervisor/writer/verify) **and** the fan-out sub-agents — not just the ReAct `"agent"` key. Decide the streaming approach (`subgraphs=True` namespacing vs. helper-accumulated return) and document it.
  - Attribute the captured spend **additively** to the **parent's super-step `checkpoint_id`** under the existing `model_token_spend` operation, via `add_cost_and_preserve_metadata` (`core/checkpoint_repository.py:127`) — **additive, never overwrite**. Still Pattern A: **no `sub_agent_id` column, no per-sub-agent rows, no per-tree rollup**.
  - The `compaction.tier3` partial-unique index is **not** in play (scoped to `operation='compaction.tier3'`), so wide fan-out poses no ledger-idempotency hazard.
- **Super-step-boundary budget pause (§A11-E2):**
  - Evaluate the budget pause at **fan-out super-step COMPLETION**, not mid-`Send`. A mid-branch pause `return` (`:3273`) abandons live sibling sub-agent branches with possibly-partial `pending_writes` and re-bills completed siblings on resume. Accept the `max_fanout × per-sub-agent ceiling` (S3 ceiling) overshoot the design already budgets — OR prove mid-super-step `pending_writes` coherence before pausing mid-branch.
  - At the boundary, `_check_budget_and_pause` (`:3796`) pauses an over-budget Supervisor task with the same semantics as ReAct (per-task → manual resume; hourly → auto-recover). The existing carve-out skips (`MEMORY_WRITE_NODE_NAME` / `compaction.tier3`, `:3160-3163`) are **not** widened for fan-out.
  - Operators size `budget_max_per_task` with the `max_fanout × ceiling` headroom; S8 does not add a new in-flight meter (the "finer in-flight metering" refinement is **deferred** — note it, don't build it — §A8).

## Affected Component

- **Service/Module:** Worker Service — graph build + cost-attribution mechanism + super-step-boundary budget pause
- **File paths:**
  - `services/worker-service/executor/graph.py` (modify — `_build_graph` topology branch; ensure `execute_task` reaches the supervisor path; **extend the `:3166` cost-recording loop to attribute every Supervisor/sub-agent LLM node's `usage_metadata` additively to the parent super-step `checkpoint_id`**; **gate the budget pause at the fan-out super-step boundary** — both are production graph-code changes, not test-only)
  - `services/worker-service/executor/supervisor/graph.py` (modify/finalize — expose the compiled Supervisor graph builder `_build_graph` calls; S5–S7 assembled nodes/edges, S8 finalizes the compile with `durability="sync"`)
  - `services/worker-service/tests/test_graph_topology_branch.py` (new — branch selection + supervisor reachability)
  - `services/worker-service/tests/test_supervisor_fanout_budget.py` (new — the wide-fan-out **non-zero cost-attribution** test + super-step-boundary pause/resume test)
  - `services/worker-service/tests/test_react_graph_unchanged.py` (new or extend — regression guard that the `react`/absent path is unchanged)
- **Change type:** modify `_build_graph` branching + finalize supervisor compile + extend the `:3166` cost-recording loop + gate the budget pause at the fan-out super-step boundary + mechanism tests

## Dependencies

- **Must complete first:** **S3..S7** — S8 compiles the whole Supervisor graph, so every node (Scope/Supervisor/fan-out/Subagent/Writer/verify) and the shared helper must exist.
- **Provides output to:** **S11** (Supervisor E2E manifest builds on a reachable, compiled supervisor path), and the deploy-order constraint with **S9** (the migration `0025` must reach prod **before** the worker emits `subagent_*`/`supervisor_iteration` events — §A6; S8 makes the emitting path reachable).
- **Shared interfaces/contracts:** the `_build_graph` topology switch; the compiled Supervisor graph builder; the **cost-attribution mechanism** (extended `:3166` loop → additive `model_token_spend` write at the parent super-step `checkpoint_id`, no `sub_agent_id`); the super-step-boundary budget-pause gate.

## Implementation Specification

### `_build_graph` topology branch

At `:1059`, read `agent_config.get("topology", "react")` and branch. Keep the ReAct path exactly as-is for `react`/absent (factor the existing body into a helper if cleaner, but preserve behavior). For `supervisor`, delegate to `executor/supervisor/graph.py`'s builder. For any other value, raise a build-time error (defensive). **Worktree-isolate** this change — S8 edits `graph.py` heavily; any parallel agent touching `graph.py` collides (§A9).

### Supervisor compile with `durability="sync"`

Finalize `executor/supervisor/graph.py` so its compiled graph is invoked with `durability="sync"`, matching `:3127`. Mirror the ReAct compile/checkpointer wiring (same Postgres checkpointer, same `thread_id` threading) — Pattern A means the Supervisor run is one durable task on the parent's `thread_id`.

### `execute_task` reachability

Confirm `execute_task` (`:2611`) routes a `topology=supervisor` agent's task into the supervisor graph via `_build_graph`. If `_build_graph` is the single construction point, this is automatic once the branch lands; add a test that a supervisor agent's task actually runs Scope→Writer.

### Cost-attribution mechanism (extend the `:3166` loop) — §A11-E1

- Extend the cost-recording loop at `graph.py:3166` (`if "agent" in event:`) so it captures `usage_metadata` / `response_metadata` from **every** LLM-bearing Supervisor node (scope/supervisor/writer/verify) **and** the fan-out sub-agents — not only the ReAct `"agent"` node key. Choose and document the streaming approach (`subgraphs=True` namespacing vs. helper-accumulated return).
- Attribute the captured spend **additively** to the **parent's super-step `checkpoint_id`** under the existing `model_token_spend` operation, via `add_cost_and_preserve_metadata` (`core/checkpoint_repository.py:127`). **Additive, never overwrite.** Pattern A: no `sub_agent_id` column, no per-sub-agent rows.
- Add a code comment at the fan-out/compile site noting the deferred "finer in-flight metering" refinement (§A8) — do not implement it.

### Super-step-boundary budget pause — §A11-E2

- Gate `_check_budget_and_pause` (`:3796`) for the supervisor path so the pause `return` (`:3273`) fires at **fan-out super-step completion**, not mid-`Send`. A mid-branch pause abandons live sibling sub-agent branches with possibly-partial `pending_writes` and re-bills completed siblings on resume. Accept the design-budgeted `max_fanout × ceiling` overshoot — OR prove mid-super-step `pending_writes` coherence.
- Do **not** widen the existing carve-out skips (`:3160-3163`) for fan-out.

### Budget mechanism tests

- `test_supervisor_fanout_budget.py`:
  - **Wide-fan-out non-zero cost test (load-bearing):** drive a Supervisor task with a large fan-out (fake models emitting known token counts per sub-agent) and assert a **NON-ZERO** `agent_cost_ledger` delta attributable to the **parent** task, recorded under `model_token_spend` at the parent super-step `checkpoint_id` (via `sum_task_cost` / ledger inspection). Assert **no** `sub_agent_id`-keyed rows and **no** per-sub-agent rollup. (A zero delta means the loop extension didn't fire — that is the production gap E1 names.)
  - **Super-step-boundary pause/resume test:** drive an over-budget Supervisor run and assert the pause fires at the **fan-out boundary** (not mid-branch), with ≥2 sub-agent branches finished in that super-step, and that the task **resumes correctly** without re-billing completed siblings.
  - Assert the `:3160-3163` carve-out is not widened.

## Acceptance Criteria

- [ ] `_build_graph` builds the **existing** ReAct graph for `topology` absent or `"react"` — behavior unchanged (regression test passes).
- [ ] `_build_graph` builds the Supervisor graph (via `executor/supervisor/graph.py`) for `topology="supervisor"`.
- [ ] An unrecognized `topology` value fails the task build defensively (not a silent fallback to ReAct).
- [ ] The Supervisor graph is compiled/invoked with `durability="sync"` (matching `:3127`) — asserted in a test (no `"async"`).
- [ ] A `topology=supervisor` agent's task runs the full Scope→Writer path from `execute_task` (`:2611`).
- [ ] **The `:3166` cost-recording loop is extended** to capture `usage_metadata` from every LLM-bearing Supervisor node + fan-out sub-agents (not only the `"agent"` node key).
- [ ] **Wide-fan-out NON-ZERO cost test:** after a multi-sub-agent run, the `agent_cost_ledger` shows a **non-zero** delta attributable to the **parent** task, recorded additively under `model_token_spend` at the parent's super-step `checkpoint_id`; there are **no** `sub_agent_id`-keyed rows and **no** per-sub-agent rollup. (A zero delta is a failing test — it means Supervisor spend is still being dropped.)
- [ ] **Super-step-boundary pause:** a budget pause fires at the **fan-out super-step boundary** (not mid-branch), with ≥2 sibling branches finished, and the task **resumes correctly** without re-billing completed siblings.
- [ ] `_check_budget_and_pause` (`:3796`) pauses an over-budget supervisor task with the same per-task / hourly semantics as ReAct; the `:3160-3163` carve-out is unchanged (not widened for fan-out).
- [ ] No new ledger exemption, no `sub_agent_id` column, no refund path introduced (grep the diff). The per-checkpoint cost write is **additive** (`add_cost_and_preserve_metadata`), never overwrite.
- [ ] Narrowest worker tests pass via the isolated harness (topology branch + cost-attribution mechanism + super-step-boundary pause + ReAct regression); `progress.md` marks S8 Done.

## Testing Requirements

- **Unit/integration tests:** branch selection (absent/`react`/`supervisor`/invalid); supervisor compile uses `durability="sync"`; `execute_task` reaches the supervisor graph; ReAct path unchanged (regression).
- **Wide-fan-out NON-ZERO cost test (the load-bearing one):** drive a Supervisor task with a large fan-out and fake models with known token counts; assert via `sum_task_cost` / ledger inspection a **non-zero** delta attributable to the parent task, recorded additively under `model_token_spend` at the parent super-step `checkpoint_id`, with no `sub_agent_id`-keyed / per-sub-agent rows. A zero delta is a failing test (Supervisor spend dropped). **Do not name a sub-graph test node `"agent"` to make this pass** — that masks the production gap E1 names (§Constraints).
- **Super-step-boundary pause/resume test:** assert over-budget → pause fires at the fan-out boundary (not mid-branch) with ≥2 finished siblings, and resume does not re-bill completed siblings.
- **Worktree-concurrency-safe:** ephemeral ports for anything binding a socket (`scripts/e2e/free-port.py` / `:0`); use the isolated DB harness — never raw `pytest tests/backend-integration` in a worktree (§A9). The cost test needs the DB, so run it via `make e2e-test PYTEST_ARGS='-k fanout_budget'`.
- **Run narrowest scope, but include the ReAct regression** since S8 edits `graph.py` heavily.

## Constraints and Guardrails

- **NO Pattern B.** No sub-agent task rows, no `parent_task_id`, no per-sub-agent leases, no `sub_agent_id` cost-ledger column, no `waiting_for_subagent` state (§A0 invariant 1). A Supervisor run is ONE task / ONE `thread_id` / ONE checkpoint stream.
- **NO `agent_config.mode` field.** Branch on `topology` only (§A0 invariant 3 / §A9).
- **Cost attribution is a BUILT mechanism, not an audit** (§A11-E1): S8 **extends** the `:3166` cost loop so Supervisor/sub-agent LLM spend is captured (without it, a Deep Research run records ~$0 and never trips the budget pause). The write stays Pattern A: NO new ledger exemption, NO `sub_agent_id` column, NO per-tree rollup, NO refund path; the per-checkpoint cost write is **additive** (`add_cost_and_preserve_metadata`), never overwrite. The carve-out skips (`:3160-3163`) must NOT be widened for fan-out.
- **Do NOT name a sub-graph test node `"agent"` to make the cost test pass.** The `:3166` loop is gated on the `"agent"` key; reusing that name in the Supervisor sub-graph would make the *existing* loop capture spend and let the wide-fan-out test go green **without** extending the loop — masking the exact production gap E1 names. The mechanism must work for real Supervisor node keys (scope/supervisor/writer/verify/fan-out). (§A11-E1)
- **Budget pause at the super-step boundary, not mid-branch** (§A11-E2): the pause is evaluated at fan-out super-step completion (accepting the design-budgeted `max_fanout × ceiling` overshoot), or only after proving mid-super-step `pending_writes` coherence. A mid-`Send` pause `return` (`:3273`) is forbidden absent that proof.
- **Do NOT switch durability to async** — Supervisor compile/invoke is `durability="sync"`, matching `:3127` (§A9).
- **Findings quotes are immutable** (§A0 invariant 4) — S8 wires nodes; it must not introduce any transform that mutates `supporting_quote`.
- **Preserve the ReAct path** — `topology` absent/`react` is byte-for-byte the current behavior; the regression test is mandatory.
- **Worktree-isolate** the `graph.py` edit (§A9) — heavy edits to a shared file.
- Do not own the `task_events` migration / `ActivityProjectionService` (S9) or Console (S10).
- **Task-timeout reachability (§A11-E7).** A whole Deep Research run is *one task*; the reaper dead-letters any task where `timeout_reference_at + task_timeout_seconds < NOW()` (`core/reaper.py:98`), and `timeout_reference_at` is set **once at creation** — so a wide, multi-iteration fan-out exceeding the default 3600s is **dead-lettered mid-run** (not a resumable pause), stranding accumulated `subagent_results`. The fix is **one of**: (a) the `research` preset raises `task_timeout_seconds` substantially — **S2's option**, the simpler one; or (b) the supervisor topology resets `timeout_reference_at` at super-step boundaries — **S8's option**, owned here only if E7 is resolved to the reset side. Pick **one** before research GA; if S8 owns the reset, document the per-step-ceiling × iterations × fanout wall-clock math that sizes it. Default to S2's preset bump unless E7 is explicitly resolved to the S8 reset. Cross-reference §A11-E7.

## Assumptions

- S3..S7 have landed: `executor/supervisor/graph.py` exposes a builder that returns a compiled (or compilable) Supervisor graph, and all nodes exist.
- `_build_graph` (`:1059`) is the single graph-construction entry that `execute_task` (`:2611`) uses; branching there makes the supervisor path reachable without other call-site edits.
- The cost ledger primitives (`insert_cost_row`/`sum_task_cost`/`sum_hourly_cost_for_agent`, `add_cost_and_preserve_metadata`) and `_check_budget_and_pause` (`:3796`) are unchanged by Agent Modes — S8 **reuses** them but **adds a new caller**: the extended `:3166` loop drives an additive `model_token_spend` write for Supervisor/sub-agent spend at the parent super-step `checkpoint_id` (§A11-E1). S8 does **not** modify the ledger schema or the pause primitive itself.
- LangGraph 1.0.5 subgraphs + `Send` compile under the Postgres checkpointer with `durability="sync"` (design verified against `langgraph==1.0.5`).
- The `agent_config.topology` key is validated/canonicalized by S1; `_build_graph`'s `.get("topology","react")` default covers pre-existing agents.

<!-- AGENT_TASK_END: task-s8-graph-build-branching.md -->
