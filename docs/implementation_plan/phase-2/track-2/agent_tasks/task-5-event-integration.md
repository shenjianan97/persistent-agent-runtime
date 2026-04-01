<!-- AGENT_TASK_START: task-5-event-integration.md -->

# Task 5 — Event Recording Integration

## Agent Instructions

You are a software engineer implementing one module of a larger system. Your scope is strictly limited to this task. Do not modify components outside the "Affected Component" listed below.

**CRITICAL PRE-WORK:** Before beginning implementation, you MUST read:
1. `docs/design/phase-2/PHASE2_MULTI_AGENT.md` — Section 5 (Execution Audit History)
2. `services/api-service/src/main/java/com/persistentagent/api/service/TaskService.java` — existing state transition methods (submitTask, cancelTask, redriveTask)
3. `services/api-service/src/main/java/com/persistentagent/api/service/TaskEventService.java` — Task 2 output (recordEvent method)
4. `services/worker-service/executor/graph.py` — execution paths (completion, retry, dead-letter)
5. `services/worker-service/core/reaper.py` — existing reaper scan queries

**CRITICAL POST-WORK:** After completing this task, update the status in `docs/implementation_plan/phase-2/track-2/progress.md` to "Done".

## Context

Tasks 2 and 4 established the event recording infrastructure (API-side `TaskEventService` and worker-side transaction-scoped event insert helper). This task wires event emission into every existing state transition so that the `task_events` table captures a complete lifecycle audit trail.

Because `task_events` is the durable lifecycle history, event recording is not best-effort. The paired task-state mutation and event INSERT must commit or roll back together.

## Task-Specific Shared Contract

- API-side events use `TaskEventService.recordEvent()` (Task 2 output) inside the same Spring transaction as the paired `tasks` mutation.
- Worker-side events use the transaction-scoped asyncpg helper from Task 4 on the same connection as the paired `tasks` mutation.
- Reaper-side events use the same asyncpg helper pattern inside the same transaction as each reaper mutation.
- All events include `status_before` and `status_after` for the transition.
- The `details` JSONB field carries event-specific context (e.g., error code, retry count, dead letter reason).

## Affected Component

- **Service/Module:** API Service + Worker Service — Event Emission
- **File paths:**
  - `services/api-service/src/main/java/com/persistentagent/api/service/TaskService.java` (modify)
  - `services/worker-service/executor/graph.py` (modify)
  - `services/worker-service/core/reaper.py` (modify)
- **Change type:** modification

## Dependencies

- **Must complete first:** Task 2 (Event Service — API-side `TaskEventService`), Task 4 (Worker Interrupt — worker-side transaction-scoped event insert helper)
- **Provides output to:** Task 6 (Console — events timeline will display these events), Task 7 (Integration Tests — verifies event sequences)
- **Shared interfaces/contracts:** Event type values from the `task_events` CHECK constraint (Task 1)

## Implementation Specification

### Step 1: API-side event emission in TaskService

Add `TaskEventService` as a constructor dependency in `TaskService`. Ensure mutating service methods that also emit events execute inside a transaction boundary so the `tasks` row change and `task_events` INSERT commit together. Add `recordEvent()` calls to:

**`submitTask()` — after successful task insertion:**
```java
taskEventService.recordEvent(
    tenantId, taskId, agentId,
    "task_submitted",
    null,        // status_before (task didn't exist)
    "queued",    // status_after
    null,        // worker_id
    null, null,  // error_code, error_message
    "{}"         // details
);
```

**`cancelTask()` — after successful status transition:**
```java
taskEventService.recordEvent(
    tenantId, taskId, agentId,
    "task_cancelled",
    previousStatus,   // status_before (from query result or known context)
    "dead_letter",    // status_after
    null,
    "cancelled_by_user", null,
    "{}"
);
```

Note: The current `cancelTask()` does not return the previous status. Modify the CTE to RETURN the previous status rather than dropping it; the audit trail should capture the real transition.

**`redriveTask()` — after successful redrive:**
```java
taskEventService.recordEvent(
    tenantId, taskId, agentId,
    "task_redriven",
    "dead_letter",  // status_before (only dead_letter tasks can be redriven)
    "queued",       // status_after
    null,
    null, null,
    "{}"
);
```

### Step 2: Worker-side event emission in GraphExecutor

Using the transaction-scoped event insert helper from Task 4, add event recording to:

**Task claim (in the poller claim transaction):**

Record `task_claimed` in the same database transaction and on the same connection that performs the `queued -> running` claim update in `core/poller.py`. Do not defer this event until `execute_task()` starts, because that would break the requirement that the claim state change and event INSERT commit or roll back together.

Right after the claim UPDATE succeeds and before that transaction commits:
```python
await self._record_task_event(
    task_id, tenant_id, agent_id,
    "task_claimed", "queued", "running", worker_id
)
```

If the current poller callback boundary makes this awkward, extend the poller claim path so it can emit the event before handing the claimed task to `execute_task()`.

**Successful completion (after the completion UPDATE):**
```python
await self._record_task_event(
    task_id, tenant_id, agent_id,
    "task_completed", "running", "completed", worker_id
)
```

**Retryable error (in `_handle_retryable_error()`):**
```python
await self._record_task_event(
    task_id, tenant_id, agent_id,
    "task_retry_scheduled", "running", "queued", worker_id,
    error_code="retryable_error",
    error_message=str(error),
    details={"retry_count": retry_count, "retry_after": str(retry_after)}
)
```

**Dead letter (in `_handle_dead_letter()`):**
```python
await self._record_task_event(
    task_id, tenant_id, agent_id,
    "task_dead_lettered", "running", "dead_letter", worker_id,
    error_code=error_code,
    error_message=error_message,
    details={"dead_letter_reason": reason}
)
```

Note: The interrupt-related events (`task_approval_requested`, `task_input_requested`) are already recorded in Task 4's `_handle_interrupt()`. HITL resumes follow the normal claim path, so the second `task_claimed` event after re-queue serves as the observable resume point.

### Step 3: Reaper-side event emission

In `reaper.py`, after each UPDATE...RETURNING scan, INSERT events for each affected task:

**Expired lease requeue (retry_count < max_retries):**
```python
for row in requeued_rows:
    await self._record_task_event(
        pool, str(row["task_id"]), row["tenant_id"], row["agent_id"],
        "task_reclaimed_after_lease_expiry", "running", "queued",
        worker_id=None  # reaper is not a specific worker
    )
```

**Expired lease dead-letter (retry_count >= max_retries):**
```python
for row in dead_lettered_rows:
    await self._record_task_event(
        pool, str(row["task_id"]), row["tenant_id"], row["agent_id"],
        "task_dead_lettered", "running", "dead_letter",
        worker_id=None,
        error_code="retries_exhausted"
    )
```

**Task timeout dead-letter:**
```python
for row in timed_out_rows:
    await self._record_task_event(
        pool, str(row["task_id"]), row["tenant_id"], row["agent_id"],
        "task_dead_lettered", status_before, "dead_letter",
        worker_id=None,
        error_code="task_timeout"
    )
```

**Human input timeout dead-letter (from Task 4's reaper addition):**
```python
for row in input_timed_out_rows:
    await self._record_task_event(
        pool, str(row["task_id"]), row["tenant_id"], row["agent_id"],
        "task_dead_lettered", status_before, "dead_letter",
        worker_id=None,
        error_code="human_input_timeout"
    )
```

Note: The reaper's existing UPDATE...RETURNING queries may need to include `tenant_id` and `agent_id` in the RETURNING clause if they don't already. Check and add as needed.

**Reaper event helper:** Add a `_record_task_event()` method to the reaper class following the same pattern as the GraphExecutor helper. Or, extract a shared utility function that both can use. The simplest approach is to duplicate the helper since it's a single INSERT statement.

## Acceptance Criteria

- [ ] `task_submitted` event recorded on every successful task submission
- [ ] `task_cancelled` event recorded on every successful cancellation
- [ ] `task_redriven` event recorded on every successful redrive
- [ ] `task_claimed` event recorded in the same transaction as the `queued` → `running` claim update
- [ ] `task_completed` event recorded on successful completion
- [ ] `task_retry_scheduled` event recorded on retryable error
- [ ] `task_dead_lettered` event recorded on dead-letter (all paths: retries exhausted, timeout, non-retryable, human input timeout)
- [ ] `task_reclaimed_after_lease_expiry` event recorded when reaper requeues
- [ ] Event writes occur atomically with the paired task-state transition
- [ ] After a full task lifecycle (submit → claim → complete), `GET /v1/tasks/{id}/events` returns the correct chronological sequence

## Testing Requirements

- **Integration tests:** Submit a task, wait for completion, verify event sequence: `task_submitted`, `task_claimed`, `task_completed`.
- **Cancel test:** Submit, cancel, verify: `task_submitted`, `task_claimed` (if claimed), `task_cancelled`.
- **Redrive test:** Dead-letter a task, redrive, verify: includes `task_redriven` event.
- **Failure test:** Verify that if `task_events` INSERT fails, the paired task-state transition is rolled back.

## Constraints and Guardrails

- Do not modify the event recording infrastructure (Task 2) or the worker interrupt handling (Task 4).
- Do not swallow event INSERT failures. Roll back the paired mutation instead.
- Do not add events for state transitions that don't actually happen (e.g., don't emit a `task_claimed` event if the claim fails).
- The `status_before` field can be `null` where the previous status is not readily available.

## Assumptions

- Task 2 has been completed (`TaskEventService.recordEvent()` is available in the API service).
- Task 4 has been completed (transaction-scoped worker event insert helper is available).
- The reaper's existing RETURNING clauses include enough data to construct events (task_id at minimum; tenant_id and agent_id may need to be added).

<!-- AGENT_TASK_END: task-5-event-integration.md -->
