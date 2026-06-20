<!-- AGENT_TASK_START: task-s11-supervisor-integration-tests.md -->

# Task S11 — Supervisor Integration + E2E Tests + Browser Scenario

## Agent Instructions

You are a software engineer implementing the **verification + manifest** task for the Supervisor topology (the Supervisor Topology track). Your scope is the test suite that proves the observable behaviors of the fan-out machinery end-to-end, plus the **scenario text** for the one Playwright check the orchestrator runs. You do **not** run Playwright or `make start` yourself.

**CRITICAL PRE-WORK:** Before beginning implementation, you MUST read:
1. `docs/design-docs/agent-modes/design.md` — sections **"Execution model: in-process fan-out (Pattern A)"**, **"Partial subagent failure"** (fail only when *zero* return), **"Citation binding"** (Writer cites by `finding_id`; verify pass flags unsupported quotes; `supporting_quote` immutable), **"Durability"** + **"Budget and redrive"** (resume-forward does NOT recompute completed siblings; operator redrive rolls back the super-step and DOES recompute → new tokens), **"Observability"** (one task row; `iteration`/`subtask` markers).
2. `docs/exec-plans/active/agent-modes/supervisor-topology/plan.md` — **§A4.1 S11 row** (the E2E manifest), **§A5 Integration Points** (the caller→callee table the tests assert against), **§A7** (the events S9 emits — tests assert they project), **§A0 invariants 1, 4, 5, 6, 7** (Pattern A; immutable findings; budget-defers-to-Track-3; `subagent_results` keyed by `subtask`; depth cap 2), **§A9 Orchestrator Guidance** (Pattern A discipline; `durability="sync"`; Console gate).
3. `docs/exec-plans/completed/phase-2/track-7/agent_tasks/task-12-integration-and-browser-tests.md` — the **shape** of an integration-test task: an **AC-to-test mapping manifest** (`test_*_ac_mapping.py`), one new worker test file per behavior, REST E2E under `tests/backend-integration/`, scenario text added to `CONSOLE_BROWSER_TESTING.md` (orchestrator runs it). Mirror this structure.
4. `services/worker-service/tests/test_track5_ac_mapping.py` — the AC-mapping-manifest precedent (asserts each behavior's concrete test file+function exists; fails descriptively when a test moves).
5. `services/worker-service/tests/test_mcp_http_integration.py` and `test_custom_tool_integration.py` — the **ephemeral-port** pattern for any test that binds a TCP port or spawns a server subprocess (`scripts/e2e/free-port.py` / bind `:0`). Worktree-concurrency-safe is mandatory.
6. `tests/backend-integration/` — existing REST E2E helpers (`helpers/api_client.py`) for the create-agent / submit-task / poll-activity flow.
7. `docs/CONSOLE_BROWSER_TESTING.md` — scenario authoring format + the orchestrator verification workflow; `docs/CONSOLE_TASK_CHECKLIST.md` — the per-task merge gate.
8. All prior Stream-S outputs (S1–S10): the config surface, `PresetDefaults`, `run_subagent` helper, `dispatch_subagent`, the Supervisor graph nodes, `_build_graph` branching, the S9 events/projection, and the S10 Console tree — these are the system under test.

**CRITICAL POST-WORK:** After completing this task:
1. Run `make worker-test` and `make e2e-test` (the isolated harness; single behaviors via `make e2e-test PYTEST_ARGS='-k supervisor_fanout'`). Run the Java/API tests if any projection assertion lives API-side. All suites green.
2. **Do NOT run Playwright or `make start`/`make stop`.** Ship the scenario text only; the **orchestrator** runs it serially after merge (AGENTS.md §Browser verification is the orchestrator's job; plan §A9 Console gate).
3. Create-or-update `docs/exec-plans/active/agent-modes/supervisor-topology/progress.md` — mark S11 done (internal status). Do **not** flip `STATUS.md` or archive the directory; the orchestrator does that after Playwright verification passes.

## Context

S11 is the Stream-S verification task. S1–S10 each land a slice (config, presets, helper, tool, graph nodes, build branching, observability, Console); S11 proves the **composed** system exhibits the design's load-bearing behaviors and that the Pattern-A invariants actually hold under crash and redrive — the parts that unit tests on individual nodes cannot catch (cross-node fan-out determinism, partial-failure proceed-vs-fail, resume-forward vs. redrive cost, budget rollup into the parent). It also ships the one browser scenario for the research-preset create flow + sub-agent tree render.

Per AGENTS.md, **a subagent ships code + worker/integration tests + scenario text; the orchestrator runs Playwright once, serially, after merge.** This task therefore writes the Playwright **scenario** into `CONSOLE_BROWSER_TESTING.md` but does not execute it.

## Task-Specific Shared Contract

- **AC mapping manifest** at `services/worker-service/tests/test_supervisor_ac_mapping.py` — one failing-when-missing meta-test per observable behavior below, asserting the concrete test file+function exists (mirrors `test_track5_ac_mapping.py`). This makes the Supervisor behavior coverage auditable.
- **New worker/integration tests** under `services/worker-service/tests/test_supervisor_*.py` for the in-graph behaviors (fan-out, partial failure, citations, resume-forward, redrive, caps, budget).
- **New REST E2E test** under `tests/backend-integration/test_supervisor_*.py` for the research-preset create flow + activity-tree projection over a real (mocked-LLM) run.
- **New Playwright scenario** in `docs/CONSOLE_BROWSER_TESTING.md` for the research-preset create + sub-agent-tree render — **orchestrator executes**.
- All LLM calls are **mocked** (fake model returning deterministic subtask lists / findings / writer output) — no live credentials. All TCP-binding tests use **ephemeral ports**.

## Observable behaviors to cover

Each row is a meta-test in the manifest + one concrete test. Assert **observable behavior**, never internal call sequences (no Pattern-B-shaped assertions).

| # | Behavior | What the test asserts (observable) |
|---|---|---|
| 1 | **Fan-out determinism** | Supervisor node emits N subtasks → the graph `Send`s exactly N sub-agents in one super-step; N `subagent_started` events appear with distinct `subtask` ids under the same `iteration`. Structural (deterministic), not LLM-emergent. |
| 2 | **Partial failure → proceed** | One sub-agent of N fails (ceiling/timeout/error) → the graph **proceeds**: a `subagent_failed{reason}` event for the failed `subtask`, findings present for the rest, Writer runs. Run does NOT error. |
| 3 | **Zero return → fail** | **All** sub-agents in an iteration fail → the task fails (design: fail-fast applies to the all-failed case alone). Assert the task reaches a terminal failure, not a silent empty report. |
| 4 | **Citation binding** | Writer cites only **resolvable** `finding_id`s; a cited id with no matching finding surfaces a render-error flag (no fabricated source). The verify pass **flags an unsupported quote** (real source, wrong claim) without mutating the finding's `supporting_quote` (immutability invariant — §A0.4). |
| 5 | **Crash resume-forward** | Save a checkpoint mid-fan-out with some sub-agents complete; simulate a worker crash + re-claim; assert completed siblings' results are **restored from `subagent_results`, not recomputed** (their mock would produce different output / a recompute counter stays flat) — only unfinished branches re-run. |
| 6 | **Operator redrive recomputes** | `rollback_last_checkpoint` on a fan-out super-step → that whole super-step **re-runs** and recomputes (the mock fires again → **new tokens** logged to the cost ledger). Contrast with #5: resume-forward reuses, redrive recomputes. |
| 7 | **Caps enforced** | `max_fanout_per_iteration` clamps an over-large subtask list (graph dispatches at most the cap; a `supervisor_iteration` event records the cap reason); `max_iterations` stops the loop (final `supervisor_iteration{decision: stop, reason}`). |
| 8 | **Budget rolls into parent** | Sub-agent LLM cost is attributed to the **parent task** under the existing `agent` operation at the super-step `checkpoint_id` — no `sub_agent_id` ledger column, no per-sub-agent rows (§A0.1). A wide fan-out writes cost rows keyed to the parent only. Pause semantics hold: over-budget **pauses** (Track 3), never silently fails (§A0.5). |

**Browser (scenario text only — orchestrator runs):**
| 9 | **Research-preset create + tree render** | Create an agent via the `research` preset (preset selector → topology locked, supervisor section visible); submit a research task; the task-detail Activity pane renders the expandable `round → sub-agent → steps` tree from `marker.subagent.*` / `marker.supervisor.iteration` events. Scenario added to `CONSOLE_BROWSER_TESTING.md`; selection-matrix + coverage-matrix updated per `CONSOLE_TASK_CHECKLIST.md`. |

## Affected Component

- **Service/Module:** Tests (worker integration, backend-integration REST E2E, Console scenario text)
- **File paths:**
  - `services/worker-service/tests/test_supervisor_ac_mapping.py` (new — manifest)
  - `services/worker-service/tests/test_supervisor_fanout.py` (new — #1)
  - `services/worker-service/tests/test_supervisor_partial_failure.py` (new — #2, #3)
  - `services/worker-service/tests/test_supervisor_citations.py` (new — #4)
  - `services/worker-service/tests/test_supervisor_resume_forward.py` (new — #5)
  - `services/worker-service/tests/test_supervisor_redrive.py` (new — #6)
  - `services/worker-service/tests/test_supervisor_caps.py` (new — #7)
  - `services/worker-service/tests/test_supervisor_budget_rollup.py` (new — #8)
  - `tests/backend-integration/test_supervisor_research_e2e.py` (new — research-preset create → run → activity-tree projection, mocked LLM)
  - `docs/CONSOLE_BROWSER_TESTING.md` (modify — add the research-preset + tree scenario; update selection matrix)
  - `docs/exec-plans/active/agent-modes/supervisor-topology/progress.md` (modify/create — mark S11 done)
- **Change type:** new tests + manifest + scenario addition + progress bookkeeping. **No source changes** — if a test reveals a defect, open a follow-up for the owning task (S1–S10); do not edit their source to make a test pass.

## Dependencies

- **Must complete first:** **S1–S10** (the full Supervisor stack: config, presets, helper, tool, graph nodes, build branching, observability events/projection, Console tree). S11 is the terminal Stream-S task in the dependency graph (plan §A3).
- **Provides output to:** the orchestrator's Playwright run; `progress.md`; (after orchestrator verification) `STATUS.md` flip + directory archive — **orchestrator-owned, not this task**.
- **Shared interfaces/contracts:** the S9 event types + `marker.*` kinds with `iteration`/`subtask` (assert projection), the `subagent_results` reducer keyed by `subtask` (assert resume-forward), the `research` preset shape from S2 (assert create flow), `_build_graph` topology branch from S8 (system under test).

## Implementation Specification

### AC mapping manifest (`test_supervisor_ac_mapping.py`)

Mirror `test_track5_ac_mapping.py`: one meta-test per behavior (#1–#9) asserting the concrete test file + function exists. For #9 (Playwright), the meta-test asserts the scenario block exists in `CONSOLE_BROWSER_TESTING.md` (grep for the scenario heading) — it does not execute the browser.

### Worker/integration tests

- Use a **fake model** (deterministic) for the Supervisor, sub-agents, Writer, and verify pass. Drive the compiled Supervisor graph (S6/S8) through `ainvoke` / `astream` with `durability="sync"` (matching `executor/graph.py:3127`) against the isolated checkpointer.
- **#5 resume-forward:** persist a checkpoint with a partial `subagent_results` (some `subtask`s done), then resume and assert the done entries are not recomputed (e.g. the fake model for a completed `subtask` is configured to raise if called again, or a per-`subtask` call counter stays at 1). **#6 redrive:** call the existing `rollback_last_checkpoint` redrive path and assert the super-step re-runs (call counter increments) and new cost-ledger rows appear.
- **#8 budget:** assert cost rows after a wide fan-out are keyed to the parent `(tenant_id, agent_id, task_id)` under the `agent` operation only — assert **absence** of any `sub_agent_id`-shaped attribution (Pattern A). Assert an over-budget fan-out **pauses** (Track 3 `waiting_for_budget`-style), not fails.
- Keep assertions **behavioral**: assert events/findings/cost-rows/terminal-state, not the internal order of node invocations.

### REST E2E (`test_supervisor_research_e2e.py`)

Against the isolated harness API: `POST /v1/agents` with the `research` preset → `POST /v1/tasks` → poll `GET /v1/tasks/{id}/activity` → assert the tree-groupable markers (`marker.subagent.started/finding/failed`, `marker.supervisor.iteration`) appear with `iteration`/`subtask`, and that it is **one task row** (no sub-agent task rows in the task list — §A0.1). Mock the LLM at the worker boundary.

### Playwright scenario (text only)

Add a scenario to `CONSOLE_BROWSER_TESTING.md` per the authoring rules in `CONSOLE_TASK_CHECKLIST.md`: create-agent via `research` preset (assert preset selector + `data-testid`s, topology locked on the detail view, supervisor config section fields), submit a task, and verify the Activity pane renders the expandable `round → sub-agent` tree. Update the change-type → scenario **selection matrix** and the agent-config **coverage matrix** (add the `supervisor` / `agent_mode` row; `topology`/`preset` render on >1 surface → Template-D parity assertions) in the same commit. **State explicitly in the scenario block that the orchestrator executes it** (subagent does not).

## Acceptance Criteria

- [ ] `test_supervisor_ac_mapping.py` lists behaviors #1–#9 and each references an existing test/scenario — all pass.
- [ ] #1 fan-out determinism: N subtasks → N `subagent_started` events, distinct `subtask` under one `iteration`.
- [ ] #2 partial failure: one failure → run proceeds, Writer runs, `subagent_failed{reason}` present.
- [ ] #3 zero return: all fail → task reaches terminal failure (not an empty report).
- [ ] #4 citations: unresolvable `finding_id` → render-error flag (no fabricated source); verify pass flags an unsupported quote; `supporting_quote` never mutated.
- [ ] #5 resume-forward: completed siblings restored from `subagent_results`, not recomputed.
- [ ] #6 redrive: super-step re-runs, new cost-ledger rows (new tokens).
- [ ] #7 caps: `max_fanout_per_iteration` clamps; `max_iterations` stops the loop with a cap-reason `supervisor_iteration` event.
- [ ] #8 budget: cost attributed to parent only (no `sub_agent_id`); over-budget pauses (not fails).
- [ ] REST E2E asserts the research run surfaces the tree markers and is **one task row**.
- [ ] `CONSOLE_BROWSER_TESTING.md` contains the research-preset + tree scenario with orchestrator-runnable steps; selection + coverage matrices updated.
- [ ] **Deferred Decisions Ledger gate (plan §A12).** The E2E run on a real-ish research workload (wide-ish fan-out, multiple iterations) **reports the deferred-item metrics** and dispositions each: **D1** — max serialized checkpoint bytes per super-step (did `checkpoint.oversized` fire?); **D2** — Writer findings-cap-hit rate + a report-quality spot check (did dropping findings degrade the report?); **D3** — observed per-task cost overshoot vs. `max_fanout × ceiling`. For each: record **Closed** (metric = evidence) or **→ named follow-up task** in §A12. **This track cannot be marked complete/archived while any §A12 row is undispositioned.**
- [ ] Confirm per-inner-turn resume + sub-agent transcript persistence **once against the real `PostgresDurableCheckpointer`** with the live worker (spikes #2–#5 proved it at the LangGraph-checkpointer level; this is the wrapper check S3 deferred to S11).
- [ ] `make worker-test` and `make e2e-test` green; `progress.md` shows S11 done.

## Testing Requirements

- All LLM calls **mocked** — no live credentials.
- **Worktree-concurrency-safe:** any test that binds a TCP port or spawns a server subprocess uses an **ephemeral/free port** (`scripts/e2e/free-port.py` or bind `:0`) — never a hardcoded port (two worktrees run the same test simultaneously). Follow the `test_mcp_http_integration.py` / `test_custom_tool_integration.py` pattern.
- Run via the **isolated harness**: `make e2e-test PYTEST_ARGS='-k ...'` for single behaviors — **never** raw `pytest tests/backend-integration` in a worktree (it hits the fixed default ports and can collide).
- Use the pinned worker venv (`services/worker-service/.venv/bin/python`) for any direct invocation.

## Constraints and Guardrails

- **No Pattern-B assertions (§A0.1):** do NOT assert (or expect) sub-agent task rows, `parent_task_id`, `sub_agent_id` ledger columns, per-sub-agent leases, or a `waiting_for_subagent` state. A test that needs any of those is testing the wrong (rejected) design. The all-failed case is **in-graph** terminal failure, not a dead-lettered sub-task.
- **Findings immutability (§A0.4):** the citation test asserts `supporting_quote` is byte-identical through reduction/selection — the reduction may drop/reorder but never rewrite a quote.
- **Budget defers to Track 3 (§A0.5):** assert pause-not-fail and no refund path; do not invent new budget exemptions beyond what S8 carves out.
- **`durability="sync"`** for the Supervisor graph in tests (match `executor/graph.py:3127`) — do not switch to LangGraph's `"async"` default.
- **Do NOT run Playwright / `make start` / `make stop`** as a subagent — orchestrator-owned (AGENTS.md; plan §A9).
- **Do NOT modify S1–S10 source** to make a test pass — open a follow-up for the owning task if a defect surfaces.
- **Do NOT flip `STATUS.md` or archive the directory** — the orchestrator does that after Playwright verification.

## Assumptions

- S1–S10 are merged and green; the `research` preset, Supervisor graph, S9 events/projection, and S10 Console tree exist as their handoff contracts (plan §A4.1) describe.
- The redrive path (`rollback_last_checkpoint`) and the isolated checkpointer used by the harness are available from the existing runtime (Track 2 / Track 3).
- `make e2e-test` brings up the per-worktree isolated DB + applies migrations (incl. S9's `0025`) automatically; the Playwright orchestrator workflow in `CONSOLE_BROWSER_TESTING.md` is the source of truth for browser verification.

<!-- AGENT_TASK_END: task-s11-supervisor-integration-tests.md -->
