<!-- AGENT_TASK_START: task-4-task-response-enrichment.md -->

# Task 4 — Task Response Enrichment

## Agent Instructions

You are a software engineer implementing one module of a larger system. Your scope is strictly limited to this task.

**CRITICAL PRE-WORK:** Before beginning implementation, you MUST read:
1. `docs/design/phase-2/track-1-agent-control-plane.md` — canonical design contract (Task response enrichment section)
2. All response records in `services/api-service/src/main/java/com/persistentagent/api/model/response/`
3. `services/api-service/src/main/java/com/persistentagent/api/repository/TaskRepository.java` — existing SELECT queries
4. `services/api-service/src/main/java/com/persistentagent/api/service/TaskService.java` — existing response mapping

**CRITICAL POST-WORK:** After completing this task, update the status in `docs/implementation_plan/phase-2/track-1/progress.md` to "Done".

## Context

Every task-facing response that currently exposes `agent_id` should also expose `agent_display_name`. The value comes from the `agent_display_name_snapshot` column in the tasks table — never from a live join to the `agents` table. This ensures historical task views remain stable even if the agent is renamed.

This task can run in parallel with Tasks 2 and 3 after Task 1 completes, since it only requires the schema column to exist.

## Task-Specific Shared Contract

- Display name must come from the snapshot column `tasks.agent_display_name_snapshot`, not from a live join to `agents`.
- The field is nullable to handle pre-Track-1 tasks gracefully (they will have NULL display names).
- Task list-style responses remain lightweight — do not add `agent_config_snapshot` to list endpoints.
- The `TaskSubmissionResponse` is handled by Task 3 — this task covers all other response types.

## Affected Component

- **Service/Module:** API Service (Java Spring Boot)
- **File paths:**
  - `services/api-service/src/main/java/com/persistentagent/api/model/response/TaskStatusResponse.java` (modify)
  - `services/api-service/src/main/java/com/persistentagent/api/model/response/TaskSummaryResponse.java` (modify)
  - `services/api-service/src/main/java/com/persistentagent/api/model/response/DeadLetterItemResponse.java` (modify)
  - `services/api-service/src/main/java/com/persistentagent/api/model/response/TaskObservabilityResponse.java` (modify)
  - `services/api-service/src/main/java/com/persistentagent/api/repository/TaskRepository.java` (modify — add column to SELECT queries)
  - `services/api-service/src/main/java/com/persistentagent/api/service/TaskService.java` (modify — pass display_name to response constructors)
- **Change type:** modification

## Dependencies

- **Must complete first:** Task 1 (schema — `agent_display_name_snapshot` column must exist)
- **Provides output to:** Task 6 (Console reads `agent_display_name` from responses)
- **Can run in parallel with:** Task 2, Task 3

## Implementation Specification

### Step 1: Add agent_display_name to response records

Add `@JsonProperty("agent_display_name") String agentDisplayName` to each response record. Place it immediately after the `agentId` field for consistency.

**TaskStatusResponse:**
```java
public record TaskStatusResponse(
    @JsonProperty("task_id") UUID taskId,
    @JsonProperty("agent_id") String agentId,
    @JsonProperty("agent_display_name") String agentDisplayName,  // NEW
    String status,
    // ... rest unchanged
) {}
```

**TaskSummaryResponse:**
```java
public record TaskSummaryResponse(
    @JsonProperty("task_id") UUID taskId,
    @JsonProperty("agent_id") String agentId,
    @JsonProperty("agent_display_name") String agentDisplayName,  // NEW
    String status,
    // ... rest unchanged
) {}
```

**DeadLetterItemResponse:**
```java
public record DeadLetterItemResponse(
    @JsonProperty("task_id") UUID taskId,
    @JsonProperty("agent_id") String agentId,
    @JsonProperty("agent_display_name") String agentDisplayName,  // NEW
    // ... rest unchanged
) {}
```

**TaskObservabilityResponse:**
```java
public record TaskObservabilityResponse(
    boolean enabled,
    @JsonProperty("task_id") UUID taskId,
    @JsonProperty("agent_id") String agentId,
    @JsonProperty("agent_display_name") String agentDisplayName,  // NEW
    String status,
    // ... rest unchanged
) {}
```

### Step 2: Update TaskRepository SELECT queries

Add `t.agent_display_name_snapshot` (or `agent_display_name_snapshot`) to the SELECT clause in:

- `findByIdAndTenant()` — add to SELECT list
- `findByIdWithAggregates()` — add to SELECT list and GROUP BY if needed
- `listTasks()` — add to SELECT list and GROUP BY clause
- `listDeadLetterTasks()` — add to SELECT list

### Step 3: Update TaskService mapping code

Update each method that constructs a response to extract and pass the display name:

**`getTaskStatus()`:**
```java
String agentDisplayName = (String) task.get("agent_display_name_snapshot");
return new TaskStatusResponse(
    (UUID) task.get("task_id"),
    (String) task.get("agent_id"),
    agentDisplayName,  // NEW
    // ... rest unchanged
);
```

**`listTasks()`:**
```java
return new TaskSummaryResponse(
    (UUID) row.get("task_id"),
    (String) row.get("agent_id"),
    (String) row.get("agent_display_name_snapshot"),  // NEW
    // ... rest unchanged
);
```

**`listDeadLetterTasks()`:**
```java
return new DeadLetterItemResponse(
    (UUID) row.get("task_id"),
    (String) row.get("agent_id"),
    (String) row.get("agent_display_name_snapshot"),  // NEW
    // ... rest unchanged
);
```

**`getTaskObservability()`:**
```java
String agentDisplayName = (String) task.get("agent_display_name_snapshot");
return new TaskObservabilityResponse(
    true,
    taskId,
    agentId,
    agentDisplayName,  // NEW
    status,
    // ... rest unchanged
);
```

## Acceptance Criteria

- [ ] `GET /v1/tasks/{id}` response includes `agent_display_name` field
- [ ] `GET /v1/tasks` list response includes `agent_display_name` in each summary item
- [ ] `GET /v1/tasks/dead-letter` response includes `agent_display_name` in each item
- [ ] `GET /v1/tasks/{id}/observability` response includes `agent_display_name` field
- [ ] For tasks created before Track 1 (no display name snapshot), the field is null in responses
- [ ] No `agent_config_snapshot` added to list endpoints (responses remain lightweight)

## Testing Requirements

- **Unit tests:** Verify response construction with non-null and null display name values. Ensure JSON serialization includes the field.
- **Integration tests:** Create agent, submit task, verify display name appears in GET task status, GET task list, and GET dead-letter responses.
- **Backward compatibility:** Insert a task row directly with NULL `agent_display_name_snapshot`, verify GET returns null without error.

## Constraints and Guardrails

- Display name must come from the snapshot column, never from a live join to `agents`.
- The field is nullable — do not default to empty string or agent_id fallback. Null is the correct representation for pre-Track-1 tasks.
- Do not add `agent_config_snapshot` to any list endpoint.
- If Task 3 has not yet landed, the `TaskSubmissionResponse` change is handled there — do not duplicate it here.

## Assumptions

- Task 1 has delivered the `agent_display_name_snapshot` column on the `tasks` table.
- The existing `TaskRepository` queries use `t.*` or explicit column lists that can be extended.
- Java records require all constructor parameters to be provided — adding a field requires updating every call site that constructs the record.

<!-- AGENT_TASK_END: task-4-task-response-enrichment.md -->
