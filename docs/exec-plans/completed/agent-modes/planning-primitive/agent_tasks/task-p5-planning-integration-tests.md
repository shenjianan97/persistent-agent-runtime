<!-- AGENT_TASK_START: task-p5-planning-integration-tests.md -->

# Task P5 — Planning Integration + Browser Tests (Planning Primitive)

## Agent Instructions

You are a software engineer implementing one module of a larger system. Your scope is strictly limited to this task.

**CRITICAL PRE-WORK:** Before beginning implementation, you MUST read:
1. `docs/design-docs/agent-modes/design.md` — **"How Planning Primitive composes"** (the plan is "injected post-compaction so it survives Track 7"; read-only `GET`; one-`in_progress` is prompt-layer). The load-bearing observable behaviors to lock in are: (a) the plan **survives a Tier-1/Tier-3 compaction** (the injected block is present post-compaction), (b) the read API shape (populated / empty / 404), (c) the checklist renders.
2. `docs/exec-plans/active/agent-modes/planning-primitive/plan.md` — §A1.5 (the P5 overview item), §A1.5 + §B row **P5** (output contract — Planning E2E: plan persists across compaction, API shape, checklist render; Playwright scenario), §A5 risk "Plan injection busts KV-cache" (P5 confirms the post-compaction injected block end-to-end; P2 owns the unit-level byte-identical check).
3. The implementations of **P1** (`plan` channel + `plan_write` — `state.py`, `tools/plan_tools.py`), **P2** (`render_plan_block` + the `agent_node` post-hook injection — `executor/graph.py`), **P3** (`GET /v1/tasks/{id}/plan` — `TaskController`/`TaskPlanService`/`TaskPlanResponse`), and **P4** (`PlanChecklist.tsx` + `data-testid="plan-item-{id}"`). P5 is an integration/E2E layer over all four — read each before asserting against it.
4. `docs/LOCAL_DEVELOPMENT.md` — the test harness + "Tracking a running task" (the `GET /v1/tasks/<id>/conversation`/activity surfaces and worker-log events) for asserting compaction fired.
5. Existing worker integration tests that exercise compaction end-to-end and the dynamic-port harness pattern in `services/worker-service/tests/test_mcp_http_integration.py` and `test_custom_tool_integration.py` — **the canonical reference for worktree-concurrency-safe ports** (bind `:0` / `scripts/e2e/free-port.py`, never a hardcoded port).

**SHARED-FILE / WORKTREE WARNING:** P5 adds **new test files** and does not modify P1–P4 source, so it has minimal shared-file surface. It still runs on the **isolated harness**; in a worktree use `make e2e-test PYTEST_ARGS='-k ...'` — **never raw `pytest tests/backend-integration`** (it hits the fixed default ports 55433/8081/18099 and collides with parallel agents, per AGENTS.md §Local Validation Notes). Any test that binds a TCP port or spawns a server uses an **ephemeral/free port** (`:0` / `scripts/e2e/free-port.py`).

**CONSOLE GATE (orchestrator split):** P5 **ships the Playwright scenario text** (in `CONSOLE_BROWSER_TESTING.md`, building on P4's scenario) and the backend/worker integration tests. As a subagent, **do NOT run `make start`/`make stop` or Playwright MCP tools** — the **orchestrator** runs the Playwright scenario once, serially, after merge. The browser leg is a BLOCKING gate the orchestrator owns.

**CRITICAL POST-WORK:** After completing this task:
1. Run the integration tests through the isolated harness: `make e2e-test PYTEST_ARGS='-k <your_test_selector>'` (and `services/worker-service/.venv/bin/python -m pytest ...` for any pure-worker integration test). Fix failures, including pre-existing ones your change surfaces.
2. Run `make console-test` if you extend any Console unit test.
3. Update the status of P5 in `docs/exec-plans/active/agent-modes/planning-primitive/progress.md` to "Done" — and, if P5 is the last Planning task to land, note the Planning Primitive track's readiness for archival per CLAUDE.md §New Phase Workflow.

## Context

P1–P4 each landed and unit-tested a slice. P5 proves the slices compose into the design's observable behaviors end-to-end: an agent writes a plan, runs long enough to trigger compaction, and the plan is **still present in the prompt after compaction** (the core durability claim); the read API returns the right shape for populated/empty/missing; and the Console checklist renders the live plan. This is the merge-gate evidence that the Planning Primitive track works as designed, not just per-unit.

## Task-Specific Shared Contract

**Observable behaviors to assert (the P5 manifest):**

1. **Plan survives Tier-1/Tier-3 compaction.** Drive a task (real or harnessed graph) whose agent calls `plan_write`, then force enough history/turns to trigger a Tier-1 mask and a Tier-3 summary (use the existing compaction-triggering test fixtures / low thresholds). Assert: after compaction fires, the LLM-bound projection still contains the injected plan `SystemMessage` (the P2 block), with the plan content intact. This is the load-bearing durability behavior — the plan written pre-compaction is not lost.
2. **`GET /v1/tasks/{id}/plan` shape — three cases:**
   - **Populated:** after the agent writes a plan, the endpoint returns `{task_id, plan:[{id,title,status},...], updated_at}` matching what was written (order + fields).
   - **Empty:** a task whose agent never called `plan_write` returns 200 `{plan: []}` (not 404).
   - **404:** a nonexistent task id returns 404.
3. **Checklist renders (browser, orchestrator-run).** The task-detail page shows the plan as a checkbox checklist: `plan-item-{id}` rows + status badges, `completed` checked; empty plan renders cleanly.

**Determinism:** use a fake/stubbed model (the worker's integration-test model harness) so `plan_write` calls and compaction triggers are deterministic — do not depend on a live LLM's choice to call the tool. Ephemeral ports throughout.

## Affected Component

- **Service/Module:** Tests — worker integration + backend-integration + Console browser scenario
- **File paths:**
  - `services/worker-service/tests/test_plan_compaction_integration.py` (new — behavior 1: plan survives Tier-1/Tier-3 compaction; pure-worker, fake model)
  - `services/worker-service/tests/backend-integration/test_plan_read_api.py` (new — behavior 2: API populated/empty/404 through the isolated API + worker) — or extend an existing backend-integration module if one already covers task sub-resources
  - `docs/CONSOLE_BROWSER_TESTING.md` (modify — finalize the Planning Playwright scenario for the orchestrator to run; behavior 3)
  - (optional) `services/console/src/features/task-detail/__tests__/PlanChecklist.test.tsx` (extend if P5 adds a render edge case beyond P4's coverage)
- **Change type:** new integration tests + finalized browser scenario

## Dependencies

- **Must complete first:** **P1, P2, P3, P4** (P5 integrates all four). Hard dependency on every prior Planning task.
- **Provides output to:** the orchestrator's merge gate for the Planning Primitive track (and the browser-verification gate it runs).
- **Shared interfaces/contracts:** asserts P1's `{id,title,status}` shape, P2's injected-block presence, P3's `TaskPlanResponse`, P4's `data-testid="plan-item-{id}"`.

## Implementation Specification

### Behavior 1 — plan survives compaction (worker integration)
- Build/seed a graph run with a low compaction threshold (reuse the existing compaction-test scaffolding), have the stub model emit a `plan_write` call early, then drive enough turns to fire Tier 1 and Tier 3. Assert the injected plan `SystemMessage` is present in the post-hook projection (the list handed to the LLM / cache strategy) on a turn after compaction fired, with plan content intact. Cross-check the worker-log compaction events (per LOCAL_DEVELOPMENT "Tracking a running task") to confirm compaction actually fired.

### Behavior 2 — read API shape (backend integration)
- Through the isolated harness (`make e2e-test`): submit a task to an agent with `plan_write` allowlisted, have the stub agent write a known plan, then `GET /v1/tasks/{id}/plan` → assert populated shape. A second task that never writes a plan → assert 200 `{plan: []}`. A random/nonexistent task id → assert 404. Ephemeral ports via the harness (`.tmp/e2e.env`); never hardcode 8081/55433.

### Behavior 3 — checklist renders (browser scenario, orchestrator-run)
- Finalize the Planning scenario in `CONSOLE_BROWSER_TESTING.md` (built on P4's): navigate to a task with a known plan, assert `plan-item-{id}` rows + status badges + checked `completed`, then assert the empty-plan task renders cleanly. Subagent writes the scenario; orchestrator executes Playwright.

## Acceptance Criteria

- [ ] An integration test proves the injected plan block is present in the LLM-bound projection **after** a Tier-1 and Tier-3 compaction fire (plan content intact).
- [ ] An integration test proves `GET /v1/tasks/{id}/plan` returns the populated shape after a write, 200 `{plan:[]}` for a no-plan task, and 404 for a missing task.
- [ ] All new tests are worktree-concurrency-safe: run via `make e2e-test PYTEST_ARGS='-k ...'` / the pinned venv, bind only ephemeral ports.
- [ ] The Planning Playwright scenario is authored in `CONSOLE_BROWSER_TESTING.md` for the orchestrator to run (subagent does not run Playwright).
- [ ] New tests pass on the isolated harness; pre-existing failures surfaced by the change are fixed.

## Testing Requirements

- **Worker integration (pinned venv):** behavior 1, deterministic via the stub-model harness + low compaction thresholds.
- **Backend integration (`make e2e-test PYTEST_ARGS='-k ...'`):** behavior 2, three response shapes, isolated harness + ephemeral ports.
- **Browser scenario text only:** behavior 3 — orchestrator runs it after merge.
- Run the narrowest selector that covers the Planning tests; do not run the whole `make test-all` unless the orchestrator asks.

## Constraints and Guardrails

- **Never raw `pytest tests/backend-integration`** in a worktree — use `make e2e-test PYTEST_ARGS='-k ...'` (fixed-port collision otherwise).
- **Ephemeral ports only** — `:0` / `scripts/e2e/free-port.py`; no hardcoded ports (worktree concurrency).
- **Do NOT run `make start`/Playwright** — orchestrator owns the browser leg.
- **Deterministic** — stub model, not a live LLM, for `plan_write` + compaction triggers.
- Assert read-only behavior holds (no mutation path exists to test against — confirm there is no PATCH/PUT endpoint).
- Do not modify P1–P4 source to make a test pass — if a real defect surfaces, route it back (systematic-debugging), don't paper over it in the test.
- Do not point any test at the dev DB (port 55432) — isolated harness only.

## Assumptions

- P1–P4 have landed and their unit tests pass.
- The worker integration harness supports a stub/fake model and configurable (low) compaction thresholds, as the existing compaction tests use.
- The isolated `make e2e-test` harness provisions the API + worker + DB on dynamic ports recorded in `.tmp/e2e.env`.
- An agent can be created with `plan_write` in its tool allowlist through the existing agent-creation path.

<!-- AGENT_TASK_END: task-p5-planning-integration-tests.md -->
