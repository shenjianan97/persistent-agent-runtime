<!-- AGENT_TASK_START: task-2-event-service.md -->

# Task 2 — Task Event Recording Infrastructure

## Agent Instructions

You are a software engineer implementing one module of a larger system. Your scope is strictly limited to this task. Do not modify components outside the "Affected Component" listed below.

**CRITICAL PRE-WORK:** Before beginning implementation, you MUST read:
1. `docs/design-docs/phase-2/design.md` — Section 5 (Execution Audit History)
2. `services/api-service/src/main/java/com/persistentagent/api/repository/LangfuseEndpointRepository.java` — pattern template for JdbcTemplate repository
3. `services/api-service/src/main/java/com/persistentagent/api/controller/TaskController.java` — existing endpoint patterns
4. `infrastructure/database/migrations/0006_runtime_state_model.sql` — task_events table schema (Task 1 output)

**CRITICAL POST-WORK:** After completing this task, update the status in `docs/exec-plans/completed/phase-2/track-2/progress.md` to "Done".

## Context

Phase 1 keeps mutable summary fields on `tasks` (status, retry_count, last_error_code) but has no audit trail for lifecycle transitions. Track 2 adds the `task_events` table (created by Task 1) and this task builds the Java service infrastructure to record and query events.

The event service is intentionally a separate service from `TaskService` because event recording will be called from both the API layer (approve/reject/respond in Task 3) and referenced as a pattern by the worker layer (Task 5). Keeping it isolated makes the dependency graph cleaner.

## Task-Specific Shared Contract

- `task_events` is the durable lifecycle audit trail. INSERT failures must propagate so the caller can roll back the paired state transition.
- Events are append-only. The service provides INSERT and SELECT only — no UPDATE or DELETE.
- The `details` JSONB field is optional (defaults to `{}`) and stores event-specific context.
- Follow the existing JdbcTemplate + raw SQL pattern used by `TaskRepository` and `LangfuseEndpointRepository`.

## Affected Component

- **Service/Module:** API Service — Event Recording
- **File paths:**
  - `services/api-service/src/main/java/com/persistentagent/api/repository/TaskEventRepository.java` (new)
  - `services/api-service/src/main/java/com/persistentagent/api/service/TaskEventService.java` (new)
  - `services/api-service/src/main/java/com/persistentagent/api/model/response/TaskEventResponse.java` (new)
  - `services/api-service/src/main/java/com/persistentagent/api/model/response/TaskEventListResponse.java` (new)
  - `services/api-service/src/main/java/com/persistentagent/api/controller/TaskController.java` (modify — add GET events endpoint)
- **Change type:** new code + modification

## Dependencies

- **Must complete first:** Task 1 (Database Migration — provides `task_events` table)
- **Provides output to:** Task 3 (HITL API — calls `TaskEventService.recordEvent()`), Task 5 (Event Integration — API-side event recording), Task 6 (Console — events timeline queries this endpoint), Task 7 (Integration Tests)
- **Shared interfaces/contracts:** `TaskEventService.recordEvent()` method signature used by Task 3 and Task 5

## Implementation Specification

### Step 1: Create TaskEventResponse record

Create `TaskEventResponse.java` as a Java record. Follow the existing response-model pattern and annotate non-trivial field names with `@JsonProperty` so the API stays snake_case:

```java
public record TaskEventResponse(
    @JsonProperty("event_id") UUID eventId,
    @JsonProperty("task_id") UUID taskId,
    @JsonProperty("agent_id") String agentId,
    @JsonProperty("event_type") String eventType,
    @JsonProperty("status_before") String statusBefore,     // nullable
    @JsonProperty("status_after") String statusAfter,       // nullable
    @JsonProperty("worker_id") String workerId,             // nullable
    @JsonProperty("error_code") String errorCode,           // nullable
    @JsonProperty("error_message") String errorMessage,     // nullable
    Object details,          // JSONB → Object (Map or null)
    @JsonProperty("created_at") OffsetDateTime createdAt
) {}
```

Also create `TaskEventListResponse.java` as a separate response record:

```java
public record TaskEventListResponse(List<TaskEventResponse> events) {}
```

### Step 2: Create TaskEventRepository

Create `TaskEventRepository.java` following the `LangfuseEndpointRepository` JdbcTemplate pattern:

**`insertEvent()` method:**
```sql
INSERT INTO task_events (tenant_id, task_id, agent_id, event_type,
                         status_before, status_after, worker_id,
                         error_code, error_message, details)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?::jsonb)
```

Parameters: `tenantId`, `taskId` (UUID), `agentId`, `eventType`, `statusBefore` (nullable), `statusAfter` (nullable), `workerId` (nullable), `errorCode` (nullable), `errorMessage` (nullable), `detailsJson` (String, defaults to `"{}"`)

**`listEvents()` method:**
```sql
SELECT event_id, tenant_id, task_id, agent_id, event_type,
       status_before, status_after, worker_id,
       error_code, error_message, details, created_at
FROM task_events
WHERE task_id = ? AND tenant_id = ?
ORDER BY created_at ASC
LIMIT ?
```

Returns `List<TaskEventResponse>`. Use a `RowMapper` to parse columns including `details` JSONB (parse via `ObjectMapper` or return as raw String/Map).

### Step 3: Create TaskEventService

Create `TaskEventService.java` as a Spring `@Service`:

**`recordEvent()` method:**
- Accepts: `tenantId`, `taskId`, `agentId`, `eventType`, `statusBefore`, `statusAfter`, `workerId`, `errorCode`, `errorMessage`, `details`
- Delegates to the repository INSERT and lets failures propagate. This service is used by callers that wrap the task-row mutation and event INSERT in a single transaction.
- Log at DEBUG level on successful INSERT.

**`listEvents()` method:**
- Accepts: `taskId` (UUID), `tenantId` (String), `limit` (int, default 100)
- Delegates to repository, returns `TaskEventListResponse`.

### Step 4: Add GET events endpoint to TaskController

Add to `TaskController.java`:

```java
@GetMapping("/{taskId}/events")
public ResponseEntity<TaskEventListResponse> getTaskEvents(
        @PathVariable UUID taskId,
        @RequestParam(defaultValue = "100") int limit) {
    TaskEventListResponse events = taskEventService.listEvents(taskId, DEFAULT_TENANT_ID, limit);
    return ResponseEntity.ok(events);
}
```

Wire `TaskEventService` as a constructor parameter in `TaskController`.

## Acceptance Criteria

- [ ] `TaskEventRepository` successfully INSERTs events into `task_events` table
- [ ] `TaskEventRepository.listEvents()` returns events in chronological order (created_at ASC)
- [ ] `TaskEventService.recordEvent()` preserves INSERT failures so callers can keep task-state changes and event writes atomic
- [ ] `GET /v1/tasks/{taskId}/events` returns `TaskEventListResponse` with events array
- [ ] GET returns empty `{ "events": [] }` for tasks with no events
- [ ] GET supports `?limit=N` parameter (default 100)
- [ ] Response JSON field names use snake_case (matching existing API conventions)

## Testing Requirements

- **Unit tests:** `TaskEventRepository` INSERT and SELECT with test database. Verify ordering. Verify limit parameter.
- **Integration tests:** Call GET endpoint for a task with no events → empty list. Insert events via repository, verify GET returns them in order.
- **Failure scenarios:** `recordEvent()` with invalid event_type → CHECK constraint violation → exception propagates to the caller so the surrounding mutation can roll back.

## Constraints and Guardrails

- Do not add UPDATE or DELETE methods. `task_events` is append-only.
- Do not call `recordEvent()` from existing flows yet — Task 5 handles integration.
- Follow the existing JSON serialization conventions in the API (snake_case field names via `@JsonProperty`, ISO 8601 timestamps).
- Do not introduce new dependencies — use the existing `JdbcTemplate`, `ObjectMapper`, and Spring annotations.

## Assumptions

- Task 1 has been completed and `task_events` table exists.
- The `DEFAULT_TENANT_ID` constant from `TaskController` is used for all queries.
- The `details` JSONB field can be returned as a raw `Map<String, Object>` or `Object` in the response.

<!-- AGENT_TASK_END: task-2-event-service.md -->
