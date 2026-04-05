<!-- AGENT_TASK_START: task-6-api-extensions.md -->

# Task 6 — Agent Budget Fields, Task Pause Fields, and Resume Endpoint

## Agent Instructions

You are a software engineer implementing one module of a larger system. Your scope is strictly limited to this task. Do not modify components outside the "Affected Component" listed below.

**CRITICAL PRE-WORK:** Before beginning implementation, you MUST read:
1. `docs/design-docs/phase-2/track-3-scheduler-and-budgets.md` — canonical design contract (API Design section, Resume endpoint, Console Design)
2. `services/api-service/src/main/java/com/persistentagent/api/repository/AgentRepository.java` — existing agent CRUD queries
3. `services/api-service/src/main/java/com/persistentagent/api/service/AgentService.java` — agent service orchestration
4. `services/api-service/src/main/java/com/persistentagent/api/controller/AgentController.java` — agent endpoints
5. `services/api-service/src/main/java/com/persistentagent/api/repository/TaskRepository.java` — `cancelTask()`, `redriveTask()`, `approveTask()` CTE patterns and `MutationResult`
6. `services/api-service/src/main/java/com/persistentagent/api/service/TaskService.java` — task service patterns
7. `services/api-service/src/main/java/com/persistentagent/api/controller/TaskController.java` — task endpoints
8. `infrastructure/database/migrations/0007_scheduler_and_budgets.sql` — Task 1 output schema

**CRITICAL POST-WORK:** After completing this task:
1. Run the full test suite with `make test` and verify all existing tests still pass. Fix any regressions before proceeding.
2. Update the status in `docs/exec-plans/active/phase-2/track-3/progress.md` to "Done".

## Context

Track 3 extends the Agent and Task APIs to expose scheduler and budget functionality. This includes:
1. Agent CRUD: expose `max_concurrent_tasks`, `budget_max_per_task`, `budget_max_per_hour` on create, update, and read
2. Task responses: expose `pause_reason`, `pause_details`, `resume_eligible_at` on task detail and list views
3. Task list filter: accept `pause_reason` as an optional filter parameter
4. Resume endpoint: `POST /v1/tasks/{task_id}/resume` for manually resuming per-task budget pauses

## Task-Specific Shared Contract

- Agent budget/concurrency fields are optional on create (defaults apply) and update (partial updates preserve existing values).
- Task pause fields are nullable — only populated for paused tasks.
- Resume endpoint follows the same CTE + `MutationResult` pattern as `approveTask()` / `rejectTask()`.
- Resume validates that the task's cumulative cost is now below the agent's `budget_max_per_task` (re-read at resume time).
- Resume returns the existing `RedriveResponse` shape: `{ task_id, status }` (two fields — do NOT add a `message` field, as this would change the existing HITL endpoint contract).
- Resume emits `task_resumed` event and `pg_notify('new_task', worker_pool_id)`.
- Agent summary responses (list view) include budget/concurrency fields.

## Affected Component

- **Service/Module:** API Service — Agent and Task CRUD + Resume
- **File paths:**
  - `services/api-service/src/main/java/com/persistentagent/api/repository/AgentRepository.java` (modify)
  - `services/api-service/src/main/java/com/persistentagent/api/service/AgentService.java` (modify)
  - `services/api-service/src/main/java/com/persistentagent/api/controller/AgentController.java` (modify — if request model changes needed)
  - `services/api-service/src/main/java/com/persistentagent/api/repository/TaskRepository.java` (modify — add resumeTask method, update queries for pause fields)
  - `services/api-service/src/main/java/com/persistentagent/api/service/TaskService.java` (modify — add resumeTask method)
  - `services/api-service/src/main/java/com/persistentagent/api/controller/TaskController.java` (modify — add resume endpoint)
  - Agent and Task response/request model files as needed (modify or new)
- **Change type:** modification + new code

## Dependencies

- **Must complete first:** Task 1 (Database Migration — agent columns and task columns exist)
- **Provides output to:** Task 7 (Console — calls these APIs), Task 8 (Integration Tests)
- **Shared interfaces/contracts:** HTTP API contract consumed by Console, integration tests, and external operators

## Implementation Specification

### Step 1: Extend Agent repository queries

**Update `insert()` in `AgentRepository`:**

Add the three new columns to the INSERT query:
```sql
INSERT INTO agents (tenant_id, agent_id, display_name, agent_config, status, max_concurrent_tasks, budget_max_per_task, budget_max_per_hour)
VALUES (?, ?, ?, ?::jsonb, ?, ?, ?, ?)
RETURNING created_at, updated_at
```

Also create the `agent_runtime_state` row in the same transaction. **This requires adding `@Transactional` to `AgentService.createAgent()`** (currently not annotated):
```sql
INSERT INTO agent_runtime_state (tenant_id, agent_id, running_task_count, hour_window_cost_microdollars, scheduler_cursor, updated_at)
VALUES (?, ?, 0, 0, '1970-01-01T00:00:00Z', NOW())
ON CONFLICT DO NOTHING
```

The `agent_runtime_state` INSERT should be a second SQL statement executed by the repository's `insert()` method, or moved to the service layer where `@Transactional` is applied.

**Update `findByIdAndTenant()` in `AgentRepository`:**

Add `max_concurrent_tasks`, `budget_max_per_task`, `budget_max_per_hour` to the SELECT list.

**Update `listByTenant()` in `AgentRepository`:**

Add the three new columns to the SELECT list so they appear in agent summary responses.

**Update `update()` in `AgentRepository`:**

Add the three new columns to the UPDATE SET clause:
```sql
UPDATE agents
SET display_name = ?, agent_config = ?::jsonb, status = ?,
    max_concurrent_tasks = ?, budget_max_per_task = ?, budget_max_per_hour = ?,
    updated_at = NOW()
WHERE tenant_id = ? AND agent_id = ?
RETURNING ...
```

### Step 2: Extend Agent service and response models

**Update agent response models** to include the three new fields. Both `AgentResponse` and `AgentSummaryResponse` are Java records and need updating:

- `AgentResponse` (in `model/response/AgentResponse.java`) — add `max_concurrent_tasks` (int), `budget_max_per_task` (long), `budget_max_per_hour` (long). This changes the constructor signature, so update all call sites:
  - `AgentService.createAgent()` (line 55)
  - `AgentService.toAgentResponse()` (line 149)
- `AgentSummaryResponse` (in `model/response/AgentSummaryResponse.java`) — add the same three fields. Update:
  - `AgentService.listAgents()` (lines 88-97) where each row is mapped to the summary record

Follow the existing `@JsonProperty` snake_case naming convention.

**Update `createAgent()` in `AgentService`:**
- Accept optional `max_concurrent_tasks`, `budget_max_per_task`, `budget_max_per_hour` from the request
- Apply defaults if not provided (5, 500000, 5000000 respectively)
- Validate: all must be > 0

**Update `updateAgent()` in `AgentService`:**
- Accept the three new fields from the request
- Pass them through to the repository update

**Update agent request model** (create/update) to include the three new optional fields with validation:
```java
@Min(1) Integer maxConcurrentTasks;
@Min(1) Long budgetMaxPerTask;
@Min(1) Long budgetMaxPerHour;
```

### Step 3: Extend Task repository and response models for pause fields

**Update `findByIdWithAggregates()` and `findByIdAndTenant()`:**

Add `pause_reason`, `pause_details`, `resume_eligible_at` to the SELECT list. Map them in the RowMapper:
- `pause_reason` → String (nullable)
- `pause_details` → Object/Map (nullable, from JSONB)
- `resume_eligible_at` → OffsetDateTime (nullable)

**Update task detail response model:**

Add fields with `@JsonProperty` snake_case:
```java
@JsonProperty("pause_reason") String pauseReason;
@JsonProperty("pause_details") Object pauseDetails;
@JsonProperty("resume_eligible_at") OffsetDateTime resumeEligibleAt;
```

**Update task list response model:**

Add `pause_reason` and `resume_eligible_at` to the list/summary response (not `pause_details` — that requires the detail endpoint).

**Update `listTasks()`:**

Add `pause_reason` as an optional filter parameter alongside `status` and `agent_id`. Follow the existing `StringBuilder` + dynamic params pattern (NOT positional `$N IS NULL OR` style):

```java
if (pauseReason != null && !pauseReason.isBlank()) {
    sql.append(" AND t.pause_reason = ?");
    params.add(pauseReason);
}
```

Validate `pause_reason` against known values (`budget_per_task`, `budget_per_hour`) similar to how `status` is validated against `VALID_TASK_STATUSES`.

Also add `t.pause_reason` and `t.resume_eligible_at` to the SELECT list AND the GROUP BY clause (since the query uses aggregation).

**Update `TaskSummaryResponse`** (in `model/response/TaskSummaryResponse.java`) to include `pause_reason` (String, nullable) and `resume_eligible_at` (OffsetDateTime, nullable). Update `TaskService.listTasks()` (lines 367-378) where the record is constructed.

**Update `TaskStatusResponse`** (in `model/response/TaskStatusResponse.java`) to include `pause_reason` (String), `pause_details` (Object, map from JSONB using `JsonParseUtil.parseJson()`), and `resume_eligible_at` (OffsetDateTime). Update `TaskService.getTaskStatus()` (lines 141-163) to pass the new fields.

### Step 4: Add resume endpoint to TaskController

```java
@PostMapping("/{taskId}/resume")
public ResponseEntity<?> resumeTask(@PathVariable UUID taskId) {
    // Delegates to TaskService.resumeTask()
    // Returns RedriveResponse shape: { task_id, status } (2 fields only — no message field)
}
```

### Step 5: Add resumeTask to TaskRepository

Follow the CTE + MutationResult pattern from `approveTask()`.

**Define a `ResumeMutationResult` record** to carry the extra diagnostic fields needed for differentiated 409 messages:
```java
public record ResumeMutationResult(
    MutationResult outcome,
    String workerPoolId,
    String agentId,
    String currentStatus,
    Long taskCost,
    Long budgetMax,
    String agentStatus
) {}
```

**Resume CTE SQL:**

```sql
WITH target AS (
    SELECT t.task_id, t.status, t.worker_pool_id, t.tenant_id, t.agent_id,
           COALESCE((SELECT SUM(cost_microdollars) FROM agent_cost_ledger WHERE task_id = t.task_id), 0) AS task_cost,
           a.budget_max_per_task, a.status AS agent_status
    FROM tasks t
    JOIN agents a ON t.tenant_id = a.tenant_id AND t.agent_id = a.agent_id
    WHERE t.task_id = ? AND t.tenant_id = ?
),
updated AS (
    UPDATE tasks t
    SET status = 'queued',
        pause_reason = NULL,
        pause_details = NULL,
        resume_eligible_at = NULL,
        version = version + 1,
        updated_at = NOW()
    FROM target tgt
    WHERE t.task_id = tgt.task_id
      AND t.status = 'paused'
      AND tgt.task_cost <= tgt.budget_max_per_task
      AND tgt.agent_status = 'active'
    RETURNING t.task_id, tgt.worker_pool_id, tgt.agent_id
)
SELECT
    (SELECT COUNT(*) FROM target) AS found,
    (SELECT COUNT(*) FROM updated) AS changed,
    (SELECT status FROM target LIMIT 1) AS current_status,
    (SELECT task_cost FROM target LIMIT 1) AS task_cost,
    (SELECT budget_max_per_task FROM target LIMIT 1) AS budget_max,
    (SELECT agent_status FROM target LIMIT 1) AS agent_status,
    (SELECT worker_pool_id FROM updated LIMIT 1) AS worker_pool_id,
    (SELECT agent_id FROM updated LIMIT 1) AS agent_id
```

The service layer uses the returned fields to determine the 409 reason:
- `found = 0` → 404 Not Found
- `current_status != 'paused'` → 409 "Task is not paused"
- `agent_status != 'active'` → 409 "Agent is disabled"
- `task_cost > budget_max` → 409 "Task cost (X) still exceeds budget (Y). Increase budget_max_per_task first."
- `changed = 1` → success

### Step 6: Add resumeTask to TaskService

```java
public RedriveResponse resumeTask(UUID taskId) {
    // 1. Call taskRepository.resumeTask(taskId, tenantId)
    // 2. Handle MutationResult: NOT_FOUND → 404, WRONG_STATE → 409 with descriptive message
    // 3. Emit pg_notify('new_task', worker_pool_id)
    // 4. Record task_resumed event via taskEventService.recordEvent(...)
    //    - event_type: task_resumed
    //    - status_before: paused
    //    - status_after: queued
    //    - details: { resume_trigger: "manual_operator_resume", budget_max_per_task_at_resume: ..., task_cost_microdollars: ... }
    // 5. Return RedriveResponse(taskId, "queued")
    //    (RedriveResponse has only 2 fields — do NOT add a message field)
}
```

The resume endpoint is idempotent: if the task has already been resumed and is now `queued`, return 409 with "Task is not paused" (the same behavior as approving a non-waiting task).

### Step 7: Update TaskController for pause_reason filter

Update the `listTasks()` endpoint to accept an optional `pause_reason` query parameter:

```java
@GetMapping
public ResponseEntity<?> listTasks(
    @RequestParam(required = false) String status,
    @RequestParam(required = false) String agent_id,
    @RequestParam(required = false) String pause_reason,
    @RequestParam(defaultValue = "50") int limit
) { ... }
```

## Acceptance Criteria

- [ ] Agent create accepts optional `max_concurrent_tasks`, `budget_max_per_task`, `budget_max_per_hour`
- [ ] Agent create applies defaults (5, 500000, 5000000) when fields not provided
- [ ] Agent create creates `agent_runtime_state` row in same transaction
- [ ] Agent update accepts and persists all three budget/concurrency fields
- [ ] Agent detail response includes `max_concurrent_tasks`, `budget_max_per_task`, `budget_max_per_hour`
- [ ] Agent list response includes `max_concurrent_tasks`, `budget_max_per_task`, `budget_max_per_hour`
- [ ] Validation rejects values ≤ 0 for all three fields
- [ ] Task detail response includes `pause_reason`, `pause_details`, `resume_eligible_at` (nullable)
- [ ] Task list response includes `pause_reason` and `resume_eligible_at`
- [ ] Task list accepts `pause_reason` filter parameter
- [ ] `POST /v1/tasks/{id}/resume` transitions `paused` task to `queued` when budget allows
- [ ] Resume returns 404 for nonexistent task
- [ ] Resume returns 409 when task is not paused
- [ ] Resume returns 409 when task cost still exceeds `budget_max_per_task`
- [ ] Resume returns 409 when agent is disabled
- [ ] Resume clears `pause_reason`, `pause_details`, `resume_eligible_at`
- [ ] Resume emits `pg_notify('new_task', worker_pool_id)`
- [ ] Resume records `task_resumed` event with manual resume details
- [ ] Resume returns `RedriveResponse` shape

## Testing Requirements

- **Unit tests:** Repository methods with test database — agent CRUD with budget fields, task pause field mapping, resume mutation with correct/wrong states. Verify validation rejects invalid budget values.
- **Integration tests:** Create agent with budget fields → verify in GET response. Update agent budget → verify persisted. Resume paused task after budget increase → verify queued. Resume while still over budget → 409.
- **Failure scenarios:** Resume on running task → 409. Resume on nonexistent → 404. Create agent with `max_concurrent_tasks = 0` → 400.

## Constraints and Guardrails

- Do not implement budget enforcement logic (pause transitions) — Task 4 handles that in the worker.
- Do not implement auto-recovery — Task 5 handles that in the reaper.
- Do not add per-task budget overrides — Track 3 uses agent-level settings only.
- Do not add bulk resume — single-task resume is sufficient for Track 3 MVP.
- Follow the existing `@JsonProperty` snake_case naming convention.
- Follow the existing CTE + MutationResult pattern for the resume mutation.

## Assumptions

- Task 1 has been completed (agent budget columns and task pause columns exist).
- A new `ResumeMutationResult` record is needed to carry diagnostic fields for differentiated 409 messages (the existing `HitlMutationResult` does not have enough fields).
- The existing `RedriveResponse` record (2 fields: `task_id`, `status`) is reused for the resume endpoint response. Do NOT add a `message` field.
- The `TaskEventService` is available for event recording.
- The `agent_cost_ledger` table exists for cumulative cost lookups in the resume validation query.

<!-- AGENT_TASK_END: task-6-api-extensions.md -->
