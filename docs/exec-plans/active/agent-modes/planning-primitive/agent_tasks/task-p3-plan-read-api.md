<!-- AGENT_TASK_START: task-p3-plan-read-api.md -->

# Task P3 — `GET /v1/tasks/{id}/plan` Read-Only API (Planning Primitive)

## Agent Instructions

You are a software engineer implementing one module of a larger system. Your scope is strictly limited to this task.

**CRITICAL PRE-WORK:** Before beginning implementation, you MUST read:
1. `docs/design-docs/agent-modes/design.md` — **"How Planning Primitive composes"** and **"What this resolves from the original Track 9 open-questions list"** → **"API surface (`GET` vs. `PATCH`): Read-only `GET /v1/tasks/{id}/plan`. Plan mutation is Workflow's surface."** This is **GET-only** — there is no PATCH/mutation surface anywhere; that is a decided design fact, not an omission.
2. `docs/exec-plans/active/agent-modes/planning-primitive/plan.md` — §A1.3 (the P3 overview item), §A4 decision **4** "Read-only API" (`GET /v1/tasks/{id}/plan` returns `{ task_id, plan: [{id, title, status, ...}], updated_at }` projected from the latest checkpoint's `plan` channel; **404 if task absent; empty plan → `{plan: []}`**), §A1.3 + §B row **P3** (output contract — `TaskPlanResponse` projected from latest checkpoint `plan`; read-only; 404 on missing task; `[]` when no plan), §A5 row "`GET .../plan` called on a supervisor-topology task … Returns `{plan: []}`" + §A2 row "Plan read API" (`TaskController` → `TaskPlanService.getPlan` → latest-checkpoint `plan` projection; missing task → 404; missing channel → `[]`).
3. `services/api-service/src/main/java/com/persistentagent/api/controller/TaskController.java` — the **sub-resource pattern** at `/{taskId}/activity` (`getTaskActivity`, the `@GetMapping("/{taskId}/activity")` method) and `/{taskId}/events`. Mirror this exactly: a thin `@GetMapping` delegating to a service, 404 via the standard not-found path. Note `getTaskActivity` delegates to `activityProjectionService.getActivity(taskId, ...)` and that `getTaskEvents` first calls `taskService.getTaskStatus(taskId)` which throws → 404 when the task is missing.
4. `services/api-service/src/main/java/com/persistentagent/api/service/ActivityProjectionService.java` — the **latest-checkpoint projection** precedent. `getActivity` (`:84`) calls `taskRepository.getLatestRootCheckpoint(taskId, tenantId)` (`:88`), reads `row.get("checkpoint_payload")`, and walks the payload's `channel_values` (`extractTurns`, see the "Turn extraction from checkpoint_payload.channel_values.messages" section ~`:240`). **P3 reads the same `checkpoint_payload` but extracts the `plan` channel instead of `messages`.** Reuse `getLatestRootCheckpoint` and the JSON-payload access pattern; do NOT re-walk messages.
5. `services/api-service/.../model/response/ActivityEventResponse.java` (the response-DTO style — records, Jackson snake_case) as the shape template for the new `TaskPlanResponse`.
6. The **P1** plan-item shape (`services/worker-service/executor/compaction/state.py` — `plan: list[dict]` of `{id, title, status}`) so the Java DTO mirrors the worker-written shape exactly. The `plan` channel lives under `checkpoint_payload.channel_values.plan` (same place `messages` lives).

**SHARED-FILE / WORKTREE WARNING:** This task is **Java-only** and does not touch any Supervisor Topology worker file or `state.py`/`graph.py`. It is safe in parallel with the worker tasks (plan.md §A3 *Cross-track coordination*: the two tracks are independent, and the Java API has zero overlap with the Python worker). It does touch `TaskController.java`; if a Supervisor Topology Java task (S1/S9) is editing controllers in parallel, check for overlap and worktree-isolate per §A3.

**CRITICAL POST-WORK:** After completing this task:
1. Run the narrowest API tests covering the change (the new `TaskPlanService` + controller test). Prefer the targeted Java test rather than the whole `make test`. For the checkpoint-projection path, a fixture `checkpoint_payload` with a `plan` channel exercises the projection without a live worker.
2. Update the status of P3 in `docs/exec-plans/active/agent-modes/planning-primitive/progress.md` to "Done".

## Context

The Console (P4) and operators need to read an agent's current plan without parsing checkpoints. P3 exposes the `plan` channel — written by P1, projected from the latest checkpoint — as a read-only REST sub-resource on the task, mirroring `GET /v1/tasks/{id}/activity`. There is deliberately **no mutation endpoint**: the design routes plan mutation to the Workflow resource (Phase 3); the ReAct scratchpad plan is written only by the agent via the `plan_write` tool (P1).

## Task-Specific Shared Contract

- **Endpoint:** `GET /v1/tasks/{taskId}/plan`. Read-only. No `POST`/`PUT`/`PATCH`/`DELETE` counterpart.
- **Response — `TaskPlanResponse`:** `{ task_id, plan: [{id, title, status}], updated_at }`.
  - `task_id` — the task id.
  - `plan` — the projected `plan` channel from the **latest root checkpoint**, items in stored order. Each item mirrors P1's `{id, title, status}`.
  - `updated_at` — the timestamp of the checkpoint the plan was projected from (the latest checkpoint's `created_at`, same source `ActivityProjectionService` uses for turn timestamps). When there is no plan, this may be the latest checkpoint's timestamp or `null` — pick one and document; `null` is acceptable when no checkpoint/plan exists.
- **404** when the task does not exist (or belongs to another tenant) — reuse the existing not-found path (`taskService.getTaskStatus(taskId)` throwing, as `getTaskEvents` does, or the projection service's own absence check — match `getActivity`'s 404 semantics).
- **Empty plan → `{plan: []}`** (HTTP 200) when the task exists but has no `plan` channel (agent never called `plan_write`, or no checkpoint yet). This is NOT a 404 — the task exists; the plan is simply empty.
- **Projection source:** the latest root checkpoint's `checkpoint_payload.channel_values.plan`. Reuse `taskRepository.getLatestRootCheckpoint(taskId, tenantId)`. Tolerate an absent `plan` key (→ `[]`) and an absent checkpoint (→ `[]`, task still 200 as long as the task row exists).

## Affected Component

- **Service/Module:** API Service — Tasks (read sub-resource)
- **File paths:**
  - `services/api-service/src/main/java/com/persistentagent/api/controller/TaskController.java` (modify — add `@GetMapping("/{taskId}/plan")`)
  - `services/api-service/src/main/java/com/persistentagent/api/service/TaskPlanService.java` (new — `getPlan(UUID taskId)` projecting the `plan` channel from the latest checkpoint)
  - `services/api-service/src/main/java/com/persistentagent/api/model/response/TaskPlanResponse.java` (new — record `{ task_id, plan, updated_at }` + a nested `PlanItem` record `{ id, title, status }`)
  - `services/api-service/src/test/java/.../TaskPlanServiceTest.java` and/or a controller test (new — populated / empty / 404 cases)
- **Change type:** new read endpoint + service + response DTO

## Dependencies

- **Must complete first:** **P1** (defines the `plan` channel + item shape this projects). P3 does not depend on P2.
- **Provides output to:** **P4** (Console `api.getTaskPlan` consumes `TaskPlanResponse`), **P5** (integration test asserts the API shape: populated, empty, 404).
- **Shared interfaces/contracts:** the `TaskPlanResponse` JSON shape (consumed verbatim by P4's TypeScript `TaskPlanResponse` type).
- **Worktree note:** Java-only; safe parallel to worker tasks. Check `TaskController.java` overlap vs. the Supervisor Topology track's S1/S9 (§A3).

## Implementation Specification

### New DTO: `TaskPlanResponse`
- Record with `task_id` (UUID/String), `plan` (`List<PlanItem>`), `updated_at` (OffsetDateTime, nullable). Nested `PlanItem` record `{ String id, String title, String status }`. Jackson snake_case, matching `ActivityEventResponse` conventions.

### New service: `TaskPlanService.getPlan(UUID taskId)`
- Resolve tenant (the `DEFAULT_TENANT_ID` constant used elsewhere in the controller).
- Throw the standard not-found exception (→ 404) when the task row is absent — match `getActivity`/`getTaskEvents`.
- Fetch the latest root checkpoint via `taskRepository.getLatestRootCheckpoint(taskId, tenantId)`. If absent → `plan = []`, `updated_at = null` (or the task's own timestamp if cheaply available — document the choice).
- Read `checkpoint_payload.channel_values.plan` (mirror `ActivityProjectionService`'s payload access; reuse any shared JSON helper it uses). Map each item dict → `PlanItem`. Absent/empty channel → `[]`.
- `updated_at` = the checkpoint's `created_at` (same `DateTimeUtil` conversion `ActivityProjectionService` uses).

### Modify: `TaskController`
- Add `@GetMapping("/{taskId}/plan")` returning `ResponseEntity<TaskPlanResponse>`, delegating to `taskPlanService.getPlan(taskId)`. Mirror `getTaskActivity`'s structure (constructor-inject the service; thin method).

### Modify: `ValidationConstants.ALLOWED_TOOLS` (added scope — P1 review finding, 2026-06-06)
- Add `"plan_write"` to `ValidationConstants.ALLOWED_TOOLS` (`services/api-service/.../config/ValidationConstants.java`). Without it, `ConfigValidationHelper.validateAllowedTools` rejects any agent config that allowlists `plan_write`, making the tool unreachable in production and dead-ending P5's "agent created with `plan_write` allowlisted" acceptance criterion. Add/extend the validation test asserting an agent config with `plan_write` in `allowed_tools` is accepted. (No other task owned this line; assigned to P3 as the track's Java task.)

## Acceptance Criteria

- [ ] `GET /v1/tasks/{taskId}/plan` for a task whose latest checkpoint has a populated `plan` channel returns 200 with `{task_id, plan:[{id,title,status},...], updated_at}` mirroring the worker-written items in order.
- [ ] `GET /v1/tasks/{taskId}/plan` for an existing task with no `plan` channel (or no checkpoint) returns 200 with `{plan: []}` (NOT 404).
- [ ] `GET /v1/tasks/{taskId}/plan` for a nonexistent task returns 404 (matching `getActivity` semantics).
- [ ] There is **no** mutation endpoint (no POST/PUT/PATCH/DELETE on `/{taskId}/plan`).
- [ ] `plan_write` is accepted by agent-config validation: it is present in `ValidationConstants.ALLOWED_TOOLS` and a test asserts an agent config allowlisting `plan_write` validates (added scope — see Implementation Specification).
- [ ] `updated_at` reflects the projected checkpoint's timestamp when a plan exists.
- [ ] Targeted Java tests pass for the populated / empty / 404 cases.

## Testing Requirements

- **Service tests (fixture checkpoint):** a fixture `checkpoint_payload` JSON with a `plan` channel → assert mapping to `TaskPlanResponse`; a payload without `plan` → `[]`; no checkpoint → `[]`; missing task → 404 path.
- **Controller test:** the three response shapes (populated 200, empty 200, missing 404).
- Run the narrowest Java scope, not the full suite. If a DB-backed integration assertion is wanted, use `make e2e-test PYTEST_ARGS='-k ...'` on the isolated harness — never raw `pytest tests/backend-integration` in a worktree.

## Constraints and Guardrails

- **GET-only** — do not add any plan-mutation surface (no PATCH/PUT/POST). Design decision: plan mutation is Workflow's surface, agent-write is via `plan_write` only.
- **Empty plan is 200 `{plan:[]}`, not 404** — only a missing *task* is 404.
- **Reuse the latest-checkpoint projection** (`getLatestRootCheckpoint` + `channel_values`) — do not invent a new checkpoint-read path; mirror `ActivityProjectionService`.
- Do not change the worker, `state.py`, or `graph.py` — Java-only.
- Do not add a new DB table/column — the plan lives in the checkpoint JSONB (plan.md §A4: "No new tables. No new columns.").
- Match existing 404 / tenant-scoping semantics; do not introduce a new error code.

## Assumptions

- `taskRepository.getLatestRootCheckpoint(taskId, tenantId)` exists and returns the row with `checkpoint_payload` + `created_at` (as used by `ActivityProjectionService:88`).
- The `plan` channel is stored under `checkpoint_payload.channel_values.plan` (same container as `messages`), serialized as a list of `{id,title,status}` dicts by the worker's checkpointer.
- The standard task-not-found → 404 path (used by `getTaskActivity`/`getTaskEvents`) is reusable.
- `DEFAULT_TENANT_ID` / `DateTimeUtil` helpers are available as in `ActivityProjectionService`.

<!-- AGENT_TASK_END: task-p3-plan-read-api.md -->
