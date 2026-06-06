<!-- AGENT_TASK_START: task-p4-console-plan-checklist.md -->

# Task P4 — Console Plan Checklist (Planning Primitive)

## Agent Instructions

You are a software engineer implementing one module of a larger system. Your scope is strictly limited to this task.

**CRITICAL PRE-WORK:** Before beginning implementation, you MUST read:
1. `docs/design-docs/agent-modes/design.md` — **"How Planning Primitive composes"** (the three plans "share UX conventions in the Console — a list with progress indicators — but not data structures") and **"What stays open … Console rendering composition with the unified activity timeline (the projection over checkpoints)"** — render the plan as a checklist on the task-detail Activity pane.
2. `docs/exec-plans/active/agent-modes/planning-primitive/plan.md` — §A1.4 (the P4 overview item), §A1.4 + §B row **P4** (output contract — Console plan checklist on task-detail; `api.getTaskPlan`; `data-testid="plan-item-{id}"` + status badge), §A2 row "Console plan checklist" + §A3 *Console gate* (Console → `GET /v1/tasks/{id}/plan`; empty/absent → render nothing, no error).
3. `docs/CONSOLE_TASK_CHECKLIST.md` — **the per-task merge gate. Read it first.** And `docs/CONSOLE_BROWSER_TESTING.md` — the canonical authoring rules, scenario templates, coverage matrix, and the change-type → scenarios selection matrix. This change adds a new render region on the task-detail page → it needs its own scenario + a coverage-matrix row, updated **in the same commit**.
4. `services/console/src/features/task-detail/ActivityPane.tsx` and `TaskDetailPage.tsx` — where the plan checklist mounts. Decide: extend `ActivityPane` or add a sibling `PlanChecklist.tsx` rendered on the task-detail page. Prefer a dedicated `PlanChecklist.tsx` component (cleaner test surface, isolates the new fetch) mounted on the task-detail page near the Activity pane.
5. `services/console/src/api/client.ts` — the task sub-resource fetch pattern: `getTaskEvents` (`:241`), `listActivity` (`:249`), `getTaskObservability` (`:134`). Add `getTaskPlan` in the same style (`fetchApi<TaskPlanResponse>(\`/v1/tasks/${taskId}/plan\`)`).
6. `services/console/src/types/index.ts` — DTO interface conventions (e.g. `TaskObservabilityResponse` `:146`). Add `TaskPlanResponse` + `PlanItem` mirroring P3's Java `TaskPlanResponse` exactly (snake_case `task_id`, `updated_at`).
7. P2's `render_plan_block` (`services/worker-service/...`) as the **format reference**: the Console checklist should present the same status semantics (checkbox state by status, `in_progress` distinguished) the model sees in the injected block — UX parity, not code sharing.

**SHARED-FILE / WORKTREE WARNING:** This task touches `services/console/src/types/index.ts` and possibly `ActivityPane.tsx` — **both shared with the Supervisor Topology track's Console task (S10)** per plan.md §A3 *Cross-track coordination* (`ActivityPane.tsx` and `types/index.ts` are two of the four files shared across the tracks; any agent editing one MUST use `isolation: "worktree"` and merge after). If S10 runs in parallel, **use `isolation: "worktree"`** and merge after. The worker/Java tasks have zero overlap with this Console-only task.

**CONSOLE GATE (orchestrator split, AGENTS.md §Browser verification is the orchestrator's job):** As the implementing subagent you **ship code + `make console-test` (unit) + scenario text** in `CONSOLE_BROWSER_TESTING.md`. **Do NOT run `make start`/`make stop` or any Playwright MCP tool** — `make start` binds global ports that collide with parallel agents. The **orchestrator** runs the Playwright scenario once, serially, after merge. Browser verification is a BLOCKING gate owned by the orchestrator, not skipped.

**CRITICAL POST-WORK:** After completing this task:
1. Run `make console-test` (Console unit tests). Fix regressions.
2. Add the new scenario to `docs/CONSOLE_BROWSER_TESTING.md` and update its coverage matrix (a Planning/plan-checklist row) in the **same commit**.
3. Update the status of P4 in `docs/exec-plans/active/agent-modes/planning-primitive/progress.md` to "Done".

## Context

The plan an agent maintains via `plan_write` (P1) is now readable at `GET /v1/tasks/{id}/plan` (P3). P4 surfaces it to the operator as a checkbox checklist on the task-detail page, so a human can see the agent's current to-do list and progress at a glance — the same "list with progress indicators" UX the design calls for across all three plan types.

## Task-Specific Shared Contract

- **Component:** a `PlanChecklist` rendered on the task-detail page (near / within the Activity pane). Fetches via a new `api.getTaskPlan(taskId)`.
- **Type:** `TaskPlanResponse { task_id: string; plan: PlanItem[]; updated_at: string | null }`, `PlanItem { id: string; title: string; status: 'pending' | 'in_progress' | 'completed' }` — mirrors P3's Java DTO field-for-field (snake_case keys).
- **Rendering:** one checkbox row per item. `completed` → checked; `pending`/`in_progress` → unchecked, with a **status badge** distinguishing each status (e.g. a small pill: "in progress" / "pending" / "completed"). Items in API order.
- **`data-testid`:** each row carries `data-testid="plan-item-{id}"` (keyed on `PlanItem.id` from P1). The status badge is assertable (its own `data-testid` or a stable text). The container carries a stable `data-testid` (e.g. `plan-checklist`) so the empty/absent state is assertable.
- **Empty / absent → render nothing (no error).** When `plan` is `[]` (or the fetch returns empty), render no checklist (or a minimal "No plan" affordance — pick one and assert it). Never surface an error for an empty plan — an agent that never called `plan_write` is the common case. A 404 (task missing) is handled by the existing task-detail not-found path, not here.
- **Read-only.** No checkboxes the operator can toggle, no edit affordance — the plan is agent-owned; there is no mutation API (P3 is GET-only).

## Affected Component

- **Service/Module:** Console — task-detail
- **File paths:**
  - `services/console/src/features/task-detail/PlanChecklist.tsx` (new — the checklist component + its `getTaskPlan` fetch hook)
  - `services/console/src/features/task-detail/TaskDetailPage.tsx` (modify — mount `PlanChecklist`) **or** `ActivityPane.tsx` (modify — if mounting inside the pane; prefer the dedicated component)
  - `services/console/src/api/client.ts` (modify — add `getTaskPlan`)
  - `services/console/src/types/index.ts` (modify — add `TaskPlanResponse` + `PlanItem`)
  - `services/console/src/features/task-detail/__tests__/PlanChecklist.test.tsx` (new — unit tests with mocked `getTaskPlan`)
  - `docs/CONSOLE_BROWSER_TESTING.md` (modify — new scenario + coverage-matrix row)
- **Change type:** new component + fetch method + type + scenario

## Dependencies

- **Must complete first:** **P3** (the `GET /v1/tasks/{id}/plan` endpoint + `TaskPlanResponse` shape this consumes). Conceptually references **P2**'s `render_plan_block` for format parity (not a code dependency).
- **Provides output to:** **P5** (browser scenario asserts the checklist renders; orchestrator runs it).
- **Shared interfaces/contracts:** consumes `TaskPlanResponse` verbatim from P3; the `data-testid="plan-item-{id}"` contract P5's Playwright scenario asserts.
- **Worktree note:** shared `ActivityPane.tsx` / `types/index.ts` with the Supervisor Topology track's S10 — see warning.

## Implementation Specification

### `types/index.ts`
- Add `PlanItem` and `TaskPlanResponse` interfaces mirroring P3's JSON exactly.

### `api/client.ts`
- Add `getTaskPlan: (taskId: string) => fetchApi<TaskPlanResponse>(\`/v1/tasks/${encodeURIComponent(taskId)}/plan\`)` in the same style as `listActivity`/`getTaskEvents`.

### `PlanChecklist.tsx`
- Fetch the plan on mount (the project's existing fetch/hook convention — match `useTaskStatus`/`useTaskObservability` or the inline `fetchApi` pattern used nearby). Render the checkbox list + status badges. Empty/absent → render nothing or a minimal "No plan" affordance. Read-only.

### Mount
- Render `PlanChecklist` on the task-detail page near the Activity pane.

## Acceptance Criteria

- [ ] A populated plan renders one checkbox row per item; `completed` items checked; each row has `data-testid="plan-item-{id}"`; each row shows a status badge.
- [ ] An empty plan (`plan: []`) renders nothing / a minimal "No plan" affordance — **no error**.
- [ ] The checklist is read-only (no togglable checkbox, no edit affordance).
- [ ] `api.getTaskPlan` calls `GET /v1/tasks/{id}/plan` and is typed `TaskPlanResponse`.
- [ ] `make console-test` passes including the new `PlanChecklist` unit tests.
- [ ] `CONSOLE_BROWSER_TESTING.md` has a new plan-checklist scenario + a coverage-matrix row, added in this commit.

## Testing Requirements

- **Unit (`make console-test`):** mock `getTaskPlan` → assert populated render (rows, `data-testid`s, badges, checked state for `completed`); empty render (no error); read-only (no interactive toggle). Follow the `__tests__/` conventions in `features/task-detail/`.
- **Scenario text only (no Playwright run here):** author the scenario in `CONSOLE_BROWSER_TESTING.md` per its templates — Scenario 1 (smoke) + a plan-checklist scenario that navigates to a task with a plan, asserts a `plan-item-{id}` row + status badge, and asserts the empty-plan case renders cleanly. **The orchestrator executes it after merge.**

## Constraints and Guardrails

- **Do NOT run `make start`/`make stop` or Playwright MCP tools** — orchestrator owns browser verification (parallel-agent port collisions). Ship scenario text only.
- **Read-only UI** — no toggle/edit; there is no mutation API (P3 GET-only).
- **Empty/absent → no error** — never error on an empty plan.
- `data-testid="plan-item-{id}"` is a contract P5 asserts — do not rename.
- Mirror P3's `TaskPlanResponse` shape exactly; do not invent client-side field renames.
- Update `CONSOLE_BROWSER_TESTING.md` (scenario + coverage matrix) in the **same commit** (CONSOLE_TASK_CHECKLIST gate).
- Worktree-isolate shared `ActivityPane.tsx` / `types/index.ts` edits against the Supervisor Topology track's S10.

## Assumptions

- P3's endpoint + `TaskPlanResponse` shape have landed.
- The task-detail page has a mount point near the Activity pane for a sibling component.
- The project's fetch convention (hook or inline `fetchApi`) is reusable as-is.

<!-- AGENT_TASK_END: task-p4-console-plan-checklist.md -->
