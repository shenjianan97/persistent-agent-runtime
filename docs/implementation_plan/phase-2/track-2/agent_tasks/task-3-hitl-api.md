<!-- AGENT_TASK_START: task-3-hitl-api.md -->

# Task 3 — Approval, Rejection, and Input Response API

## Agent Instructions

You are a software engineer implementing one module of a larger system. Your scope is strictly limited to this task. Do not modify components outside the "Affected Component" listed below.

**CRITICAL PRE-WORK:** Before beginning implementation, you MUST read:
1. `docs/design/phase-2/PHASE2_MULTI_AGENT.md` — Section 7 (Human-in-the-Loop Input)
2. `services/api-service/src/main/java/com/persistentagent/api/repository/TaskRepository.java` — existing `cancelTask()` and `redriveTask()` CTE patterns
3. `services/api-service/src/main/java/com/persistentagent/api/service/TaskService.java` — orchestration and MutationResult handling patterns
4. `services/api-service/src/main/java/com/persistentagent/api/controller/TaskController.java` — endpoint patterns
5. `infrastructure/database/migrations/0006_runtime_state_model.sql` — new columns and statuses (Task 1 output)

**CRITICAL POST-WORK:** After completing this task, update the status in `docs/implementation_plan/phase-2/track-2/progress.md` to "Done".

## Context

Track 2 introduces human-in-the-loop workflows. When a task enters `waiting_for_approval` or `waiting_for_input`, a human must approve, reject, or respond before the task can resume. This task builds the API endpoints that enable these actions.

Per the stateless Phase 2 HITL model, waiting tasks release their lease while paused. These endpoints persist the human decision/response, clear the timeout metadata, move the task back to `queued`, and emit the existing `new_task` notification so any available worker can claim it and resume from the LangGraph checkpoint with `Command(resume=...)`.

## Task-Specific Shared Contract

- Approve/reject/respond use the same CTE + `MutationResult` pattern as `cancelTask()` and `redriveTask()`.
- The API persists the resume payload and transitions the task from its waiting state back to `queued`.
- `human_response` stores a documented HITL resume payload for pickup on resume.
- Resume reuses the existing `pg_notify('new_task', worker_pool_id)` path rather than introducing a worker-specific wake mechanism.
- Each action records a task event via `TaskEventService` (from Task 2).
- Cancel is expanded to accept waiting states as valid source states.

## Affected Component

- **Service/Module:** API Service — HITL Endpoints
- **File paths:**
  - `services/api-service/src/main/java/com/persistentagent/api/repository/TaskRepository.java` (modify)
  - `services/api-service/src/main/java/com/persistentagent/api/service/TaskService.java` (modify)
  - `services/api-service/src/main/java/com/persistentagent/api/controller/TaskController.java` (modify)
  - `services/api-service/src/main/java/com/persistentagent/api/model/request/TaskRejectRequest.java` (new)
  - `services/api-service/src/main/java/com/persistentagent/api/model/request/TaskRespondRequest.java` (new)
- **Change type:** modification + new code

## Dependencies

- **Must complete first:** Task 1 (Database Migration — new statuses and columns), Task 2 (Event Service — `TaskEventService.recordEvent()`)
- **Provides output to:** Task 4 (Worker Interrupt — API contract for resume semantics), Task 6 (Console — calls these endpoints), Task 7 (Integration Tests)
- **Shared interfaces/contracts:** HTTP endpoint contract consumed by Console and integration tests

## Implementation Specification

### Step 1: Create request records

**`TaskRejectRequest.java`:**
```java
public record TaskRejectRequest(
    @NotBlank String reason
) {}
```

**`TaskRespondRequest.java`:**
```java
public record TaskRespondRequest(
    @NotBlank String message
) {}
```

### Step 2: Add repository methods

All three methods follow the CTE pattern from `cancelTask()`:

**`approveTask(taskId, tenantId)`:**
```sql
WITH target AS (
    SELECT task_id, status, worker_pool_id
    FROM tasks
    WHERE task_id = ? AND tenant_id = ?
),
updated AS (
    UPDATE tasks t
    SET status = 'queued',
        human_response = ?,
        pending_approval_action = NULL,
        human_input_timeout_at = NULL,
        lease_owner = NULL,
        lease_expiry = NULL,
        version = version + 1,
        updated_at = NOW()
    FROM target tgt
    WHERE t.task_id = tgt.task_id
      AND t.status = 'waiting_for_approval'
    RETURNING t.task_id, tgt.worker_pool_id
)
SELECT
    (SELECT COUNT(*) FROM target) AS found,
    (SELECT COUNT(*) FROM updated) AS changed,
    (SELECT worker_pool_id FROM updated LIMIT 1) AS worker_pool_id
```

The resume payload for approval should be a documented structured JSON value serialized into `human_response`, for example `{"kind":"approval","approved":true}`. Do not use an undocumented magic string sentinel.

The repository method should only perform the row mutation and return `MutationResult.UPDATED`, `WRONG_STATE`, or `NOT_FOUND` plus `worker_pool_id` for the service layer. The service method owns the single `pg_notify('new_task', worker_pool_id)` wake-up after a successful update.

**`rejectTask(taskId, tenantId, reason)`:**
```sql
WITH target AS (
    SELECT task_id, status, worker_pool_id
    FROM tasks
    WHERE task_id = ? AND tenant_id = ?
),
updated AS (
    UPDATE tasks t
    SET status = 'queued',
        human_response = ?,
        pending_approval_action = NULL,
        human_input_timeout_at = NULL,
        lease_owner = NULL,
        lease_expiry = NULL,
        version = version + 1,
        updated_at = NOW()
    FROM target tgt
    WHERE t.task_id = tgt.task_id
      AND t.status = 'waiting_for_approval'
    RETURNING t.task_id, tgt.worker_pool_id
)
SELECT
    (SELECT COUNT(*) FROM target) AS found,
    (SELECT COUNT(*) FROM updated) AS changed,
    (SELECT worker_pool_id FROM updated LIMIT 1) AS worker_pool_id
```

The `human_response` field stores a documented structured JSON value such as `{"kind":"approval","approved":false,"reason":"..."}`. The resumed worker reads it and injects the rejection into the approval-gate interrupt contract.

**`respondToTask(taskId, tenantId, message)`:**
```sql
WITH target AS (
    SELECT task_id, status, worker_pool_id
    FROM tasks
    WHERE task_id = ? AND tenant_id = ?
),
updated AS (
    UPDATE tasks t
    SET status = 'queued',
        human_response = ?,
        pending_input_prompt = NULL,
        human_input_timeout_at = NULL,
        lease_owner = NULL,
        lease_expiry = NULL,
        version = version + 1,
        updated_at = NOW()
    FROM target tgt
    WHERE t.task_id = tgt.task_id
      AND t.status = 'waiting_for_input'
    RETURNING t.task_id, tgt.worker_pool_id
)
SELECT
    (SELECT COUNT(*) FROM target) AS found,
    (SELECT COUNT(*) FROM updated) AS changed,
    (SELECT worker_pool_id FROM updated LIMIT 1) AS worker_pool_id
```

For input requests, store a documented JSON payload such as `{"kind":"input","message":"..."}` so the worker can decode it before calling `Command(resume=...)`.

### Step 3: Add service methods

In `TaskService.java`:

**`approveTask(taskId)`:**
1. Call `taskRepository.approveTask(taskId, tenantId)`
2. Handle `MutationResult`: NOT_FOUND → 404, WRONG_STATE → 409 ("Task is not waiting for approval")
3. Emit `pg_notify('new_task', worker_pool_id)` using the task's existing worker pool
4. Record event via `taskEventService.recordEvent(...)` with event_type `task_approved`, status_before `waiting_for_approval`, status_after `queued`
5. Return success response

**`rejectTask(taskId, reason)`:**
1. Call `taskRepository.rejectTask(taskId, tenantId, reason)`
2. Handle `MutationResult` same as above ("Task is not waiting for approval")
3. Emit `pg_notify('new_task', worker_pool_id)` using the task's existing worker pool
4. Record event `task_rejected` with details `{ "reason": reason }` and status_after `queued`
5. Return success response

**`respondToTask(taskId, message)`:**
1. Call `taskRepository.respondToTask(taskId, tenantId, message)`
2. Handle `MutationResult` ("Task is not waiting for input")
3. Emit `pg_notify('new_task', worker_pool_id)` using the task's existing worker pool
4. Record event `task_input_received` with details `{ "message_length": message.length() }` and status_after `queued`
5. Return success response

### Step 4: Add controller endpoints

```java
@PostMapping("/{taskId}/approve")
public ResponseEntity<?> approveTask(@PathVariable UUID taskId) {
    // returns 200 on success, 404/409 on error
}

@PostMapping("/{taskId}/reject")
public ResponseEntity<?> rejectTask(
        @PathVariable UUID taskId,
        @Valid @RequestBody TaskRejectRequest request) {
    // returns 200 on success, 404/409 on error
}

@PostMapping("/{taskId}/respond")
public ResponseEntity<?> respondToTask(
        @PathVariable UUID taskId,
        @Valid @RequestBody TaskRespondRequest request) {
    // returns 200 on success, 404/409 on error
}
```

### Step 5: Expand cancelTask to accept waiting states

Update the `cancelTask()` SQL in `TaskRepository` to accept the new states:

Change:
```sql
AND t.status IN ('queued', 'running')
```
To:
```sql
AND t.status IN ('queued', 'running', 'waiting_for_approval', 'waiting_for_input', 'paused')
```

Also update the cancel logic to clear HITL-specific fields when cancelling from a waiting state:
```sql
pending_input_prompt = NULL,
pending_approval_action = NULL,
human_input_timeout_at = NULL,
human_response = NULL,
```

### Step 6: Update TaskStatusResponse with new fields

Add to the existing `TaskStatusResponse` record (or whichever response is used for task detail), following the existing explicit `@JsonProperty` snake_case pattern:
- `@JsonProperty("pending_input_prompt") String pendingInputPrompt` (nullable)
- `@JsonProperty("pending_approval_action") Object pendingApprovalAction` (nullable)
- `@JsonProperty("human_input_timeout_at") OffsetDateTime humanInputTimeoutAt` (nullable)

Update the `findByIdWithAggregates` and `findByIdAndTenant` queries to SELECT these new columns and map them in the RowMapper.

### Step 7: Update task list query

Ensure `listTasks()` handles the new statuses correctly in the status filter parameter. The existing filter accepts a status string — verify it passes through to the SQL `WHERE status = ?` without validation against a hardcoded list (or update the validation list to include new statuses).

## Acceptance Criteria

- [ ] `POST /v1/tasks/{id}/approve` stores the documented approval resume payload, transitions the task to `queued`, and returns 200
- [ ] `POST /v1/tasks/{id}/approve` returns 409 when task is not in `waiting_for_approval`
- [ ] `POST /v1/tasks/{id}/approve` returns 404 for nonexistent task
- [ ] `POST /v1/tasks/{id}/reject` stores the documented rejection resume payload, transitions the task to `queued`, and returns 200
- [ ] `POST /v1/tasks/{id}/reject` requires non-blank `reason` in body
- [ ] `POST /v1/tasks/{id}/respond` stores the documented input resume payload, transitions the task to `queued`, and returns 200
- [ ] `POST /v1/tasks/{id}/respond` requires non-blank `message` in body
- [ ] Approve/reject/respond clear any waiting-state `lease_owner` / `lease_expiry`
- [ ] Successful approve/reject/respond reuses the normal claim poller path
- [ ] Successful approve/reject/respond emits `pg_notify('new_task', worker_pool_id)`
- [ ] All three endpoints record appropriate task events via `TaskEventService`
- [ ] `POST /v1/tasks/{id}/cancel` now accepts `waiting_for_approval`, `waiting_for_input`, `paused` states
- [ ] Task detail response includes `pending_input_prompt`, `pending_approval_action`, `human_input_timeout_at`
- [ ] Task list status filter works for new statuses

## Testing Requirements

- **Unit tests:** Repository methods with test database — correct transitions, MutationResult handling, lease release, and `worker_pool_id` return value. Service-level tests should verify the single `pg_notify('new_task', worker_pool_id)` emission after a successful update.
- **Integration tests:** End-to-end HTTP calls for approve, reject, respond with correct and wrong states.
- **Failure scenarios:** approve on `running` → 409, approve on nonexistent → 404, reject with blank reason → 400, respond on `waiting_for_approval` → 409.

## Constraints and Guardrails

- Do not implement worker-side resume logic — Task 4 handles that.
- Do not emit events from existing flows (submit, cancel, redrive) — Task 5 handles that.
- Follow the existing error response format (error message string in response body).
- The `human_response` column stores a documented JSON resume payload serialized as text. Do not use ad hoc magic strings or raw free-form payloads that the worker cannot decode reliably.

## Assumptions

- Task 1 has been completed (new statuses and columns exist).
- Task 2 has been completed (`TaskEventService` is available for injection).
- The existing `MutationResult` enum (UPDATED, WRONG_STATE, NOT_FOUND) is sufficient for all new operations.

<!-- AGENT_TASK_END: task-3-hitl-api.md -->
