<!-- AGENT_TASK_START: task-3-task-submission-refactor.md -->

# Task 3 — Task Submission Refactor

## Agent Instructions

You are a software engineer implementing one module of a larger system. Your scope is strictly limited to this task.

**CRITICAL PRE-WORK:** Before beginning implementation, you MUST read:
1. `docs/design-docs/phase-2/track-1-agent-control-plane.md` — canonical design contract (Task submission contract change section)
2. `services/api-service/src/main/java/com/persistentagent/api/service/TaskService.java` — current submission logic
3. `services/api-service/src/main/java/com/persistentagent/api/model/request/TaskSubmissionRequest.java` — current request model
4. `services/api-service/src/main/java/com/persistentagent/api/repository/TaskRepository.java` — current `insertTask()` method

**CRITICAL POST-WORK:** After completing this task, update the status in `docs/exec-plans/completed/phase-2/track-1/progress.md` to "Done".

## Context

Task submission switches from inline agent config to stored-agent-based submission. The client sends only `agent_id` plus task-level fields (input, retries, max_steps, timeout, langfuse_endpoint_id). The API resolves the agent from the `agents` table, validates it is `active`, and snapshots both its config and display name onto the task row.

This is a breaking change to the public API contract.

## Task-Specific Shared Contract

- The public task submission contract becomes agent-based only. Inline `agent_config` is removed.
- The API must resolve the agent by `agent_id`, validate it is `active`, and snapshot the resolved config.
- Agent config defaults (temperature, allowed_tools) are already canonicalized at agent creation/update time (Task 2). Submission snapshots the stored config as-is — no default re-application needed.
- Re-validating that the model is still active at submission time is a safety check worth keeping.
- `display_name` is snapshotted into `tasks.agent_display_name_snapshot` at submission time.

## Affected Component

- **Service/Module:** API Service (Java Spring Boot)
- **File paths:**
  - `services/api-service/src/main/java/com/persistentagent/api/model/request/TaskSubmissionRequest.java` (modify)
  - `services/api-service/src/main/java/com/persistentagent/api/service/TaskService.java` (modify)
  - `services/api-service/src/main/java/com/persistentagent/api/repository/TaskRepository.java` (modify)
  - `services/api-service/src/main/java/com/persistentagent/api/model/response/TaskSubmissionResponse.java` (modify)
- **Change type:** modification

## Dependencies

- **Must complete first:** Task 1 (schema — `agent_display_name_snapshot` column), Task 2 (AgentRepository + ConfigValidationHelper)
- **Provides output to:** Task 6 (Console submit page uses new contract), Task 7 (integration tests use new contract)
- **Shared interfaces/contracts:** `AgentRepository.findByIdAndTenant()` from Task 2. `ConfigValidationHelper` from Task 2.

## Implementation Specification

### Step 1: Modify TaskSubmissionRequest

Remove the `agentConfig` field and its `@Valid` annotation. The record becomes:

```java
public record TaskSubmissionRequest(
    @JsonProperty("tenant_id") String tenantId,

    @NotBlank(message = "agent_id is required")
    @Size(max = 64, message = "agent_id must not exceed 64 characters")
    @JsonProperty("agent_id") String agentId,

    @NotBlank(message = "input is required")
    @Size(max = 102400, message = "input must not exceed 100KB")
    String input,

    @Min(value = 0, message = "max_retries must be >= 0")
    @Max(value = 10, message = "max_retries must be <= 10")
    @JsonProperty("max_retries") Integer maxRetries,

    @Min(value = 1, message = "max_steps must be >= 1")
    @Max(value = 1000, message = "max_steps must be <= 1000")
    @JsonProperty("max_steps") Integer maxSteps,

    @Min(value = 1, message = "task_timeout_seconds must be >= 1")
    @Max(value = 86400, message = "task_timeout_seconds must be <= 86400")
    @JsonProperty("task_timeout_seconds") Integer taskTimeoutSeconds,

    @JsonProperty("langfuse_endpoint_id") UUID langfuseEndpointId
) {}
```

Remove the import for `AgentConfigRequest` if it is no longer referenced here.

### Step 2: Modify TaskService.submitTask()

Inject `AgentRepository` into `TaskService` constructor.

**IMPORTANT — Atomicity requirement:** The agent resolution and task insertion must happen within a single database transaction to prevent a concurrent `PUT /v1/agents/{id}` from disabling or editing the agent between the status check and the INSERT. The current codebase has no `@Transactional` annotations, so this must be explicitly added.

There are two valid approaches:

**Approach A (preferred): Atomic INSERT...SELECT in TaskRepository**

Combine agent resolution and task insertion into a single SQL statement in `TaskRepository`. This avoids introducing Spring's `@Transactional` machinery and keeps atomicity at the SQL level, consistent with the existing codebase pattern:

```java
// In TaskRepository — new method
public Optional<Map<String, Object>> insertTaskFromAgent(
        String tenantId, String agentId, String workerPoolId,
        String input, int maxRetries, int maxSteps, int taskTimeoutSeconds,
        UUID langfuseEndpointId) {
    String sql = """
        WITH agent AS (
            SELECT a.agent_id, a.display_name, a.agent_config
            FROM agents a
            -- Join models to atomically verify the agent's model is still active.
            -- This prevents enqueueing tasks against deactivated models.
            JOIN models m
              ON m.provider_id = a.agent_config->>'provider'
             AND m.model_id   = a.agent_config->>'model'
             AND m.is_active  = true
            WHERE a.tenant_id = ? AND a.agent_id = ? AND a.status = 'active'
        ),
        inserted AS (
            INSERT INTO tasks (tenant_id, agent_id, agent_config_snapshot, worker_pool_id,
                               input, max_retries, max_steps, task_timeout_seconds, status,
                               langfuse_endpoint_id, agent_display_name_snapshot)
            SELECT ?, a.agent_id, a.agent_config, ?,
                   ?, ?, ?, ?, 'queued',
                   ?, a.display_name
            FROM agent a
            RETURNING task_id, agent_display_name_snapshot, created_at
        ),
        notified AS (
            SELECT pg_notify('new_task', ?)
            FROM inserted
        )
        SELECT i.task_id, i.agent_display_name_snapshot, i.created_at
        FROM inserted i
        LEFT JOIN notified n ON true
        """;
    List<Map<String, Object>> results = jdbcTemplate.queryForList(sql,
        tenantId, agentId,
        tenantId, workerPoolId,
        input, maxRetries, maxSteps, taskTimeoutSeconds,
        langfuseEndpointId,
        workerPoolId);
    return results.isEmpty() ? Optional.empty() : Optional.of(results.get(0));
}
```

The `agent` CTE joins `agents` with `models` on `provider_id`/`model_id` and requires `is_active = true`. This atomically enforces three conditions in a single statement:
1. Agent exists
2. Agent status is `active`
3. Agent's model is still active in the models registry

If any condition fails, the CTE produces zero rows, the INSERT inserts nothing, and the method returns `Optional.empty()`. The caller then does a follow-up lookup to distinguish the failure reason (agent not found, agent disabled, or model deactivated).

**Approach B (alternative): @Transactional on TaskService.submitTask()**

If the atomic SQL approach proves too complex for handling defaults/validation, wrap `submitTask()` with `@Transactional` and use `SELECT ... FOR SHARE` when reading the agent row to prevent concurrent status changes:

```java
@Transactional
public TaskSubmissionResponse submitTask(TaskSubmissionRequest request) { ... }
```

With a locking read in AgentRepository:
```java
public Optional<Map<String, Object>> findByIdAndTenantForShare(String tenantId, String agentId) {
    String sql = """
        SELECT * FROM agents
        WHERE tenant_id = ? AND agent_id = ?
        FOR SHARE
        """;
    // ...
}
```

**Recommended: Approach A.** It keeps the codebase's existing pattern of SQL-level atomicity without introducing transaction management overhead.

Replace the current inline config logic with agent resolution:

```java
public TaskSubmissionResponse submitTask(TaskSubmissionRequest request) {
    String tenantId = ValidationConstants.DEFAULT_TENANT_ID;
    String workerPoolId = ValidationConstants.DEFAULT_WORKER_POOL_ID;

    // 1. Validate task-level fields first (cheap, no DB needed)
    validateTaskTimeoutSeconds(request.taskTimeoutSeconds());
    if (request.langfuseEndpointId() != null) {
        langfuseEndpointRepository.findByIdAndTenant(request.langfuseEndpointId(), tenantId)
            .orElseThrow(() -> new ValidationException(
                "langfuse_endpoint_id not found: " + request.langfuseEndpointId()));
    }

    // 2. Apply task-level defaults
    int maxRetries = request.maxRetries() != null ? request.maxRetries() : ValidationConstants.DEFAULT_MAX_RETRIES;
    int maxSteps = request.maxSteps() != null ? request.maxSteps() : ValidationConstants.DEFAULT_MAX_STEPS;
    int taskTimeoutSeconds = request.taskTimeoutSeconds() != null
        ? request.taskTimeoutSeconds() : ValidationConstants.DEFAULT_TASK_TIMEOUT_SECONDS;

    // 3. Atomic agent resolution + model validation + task insertion (single SQL statement)
    //    The INSERT...SELECT joins agents with models to atomically enforce:
    //    - Agent exists and status = 'active'
    //    - Agent's model is active in the models registry
    //    This prevents both TOCTOU races from concurrent agent updates and
    //    enqueueing tasks against deactivated models.
    Optional<Map<String, Object>> result = taskRepository.insertTaskFromAgent(
        tenantId, request.agentId(), workerPoolId,
        request.input(), maxRetries, maxSteps, taskTimeoutSeconds,
        request.langfuseEndpointId());

    if (result.isEmpty()) {
        // Atomic INSERT returned empty — determine why for the error response.
        Optional<Map<String, Object>> agent = agentRepository.findByIdAndTenant(tenantId, request.agentId());
        if (agent.isEmpty()) {
            throw new AgentNotFoundException(request.agentId());
        }
        String agentStatus = (String) agent.get().get("status");
        if (!"active".equals(agentStatus)) {
            throw new ValidationException(
                "Agent is disabled and cannot be used for task submission: " + request.agentId());
        }
        // Agent exists and is active, so the model must be deactivated
        throw new ValidationException(
            "Agent's model is no longer active. Update the agent's model before submitting tasks: " + request.agentId());
    }

    Map<String, Object> row = result.get();
    UUID taskId = (UUID) row.get("task_id");
    String displayName = (String) row.get("agent_display_name_snapshot");
    OffsetDateTime createdAt = toOffsetDateTime(row.get("created_at"));

    return new TaskSubmissionResponse(taskId, request.agentId(), displayName, "queued", createdAt);
}
```

**Note on agent_config defaults:** The atomic INSERT...SELECT snapshots `agent_config` directly from the agents table. This means defaults (temperature, allowed_tools) must be applied at agent creation/update time (Task 2), not at submission time. Task 2's `AgentService.createAgent()` and `updateAgent()` canonicalize nullable fields before storing.

**Note on model validation:** Model activity is now checked atomically inside the same SQL statement via a JOIN to the `models` table (`m.is_active = true`). This eliminates the TOCTOU race where a model could be deactivated between a pre-flight check and the INSERT. The follow-up lookup after an empty result distinguishes three cases: agent not found (404), agent disabled (400), or model deactivated (400 with specific message).

### Step 3: Add TaskRepository.insertTaskFromAgent()

Add the new atomic method as specified in Step 2 (Approach A). The existing `insertTask()` method can be kept for backward compatibility during the transition but should be marked as deprecated or removed once all callers have migrated.

The key SQL pattern is `INSERT INTO tasks ... SELECT ... FROM agents WHERE status = 'active'`, which atomically resolves the agent and inserts the task in a single statement. If the agent doesn't exist or is disabled, zero rows are inserted and the method returns `Optional.empty()`.

### Step 4: Modify TaskSubmissionResponse

Add `agent_display_name` field:

```java
public record TaskSubmissionResponse(
    @JsonProperty("task_id") UUID taskId,
    @JsonProperty("agent_id") String agentId,
    @JsonProperty("agent_display_name") String agentDisplayName,
    String status,
    @JsonProperty("created_at") OffsetDateTime createdAt
) {}
```

### Step 5: Clean up TaskService

Remove the now-unused private `validateModel()` and `validateAllowedTools()` methods from `TaskService` if they have been fully replaced by `ConfigValidationHelper` (from Task 2). Keep `validateTaskTimeoutSeconds()` as it remains task-specific.

## Acceptance Criteria

- [ ] `POST /v1/tasks` no longer accepts `agent_config` in the request body
- [ ] `POST /v1/tasks` with valid `agent_id` resolves agent config, snapshots config + display_name, creates task
- [ ] `POST /v1/tasks` with unknown `agent_id` returns 404
- [ ] `POST /v1/tasks` with disabled agent returns 400 with clear error message
- [ ] Agent resolution, model validation, and task insertion are atomic — a single SQL statement enforces agent active, model active, and inserts the task
- [ ] Submission with a deactivated model returns 400 with a clear message about the model being inactive
- [ ] Task row in DB has `agent_config_snapshot` from the resolved agent config
- [ ] Task row in DB has `agent_display_name_snapshot` from the agent's `display_name`
- [ ] Submission response includes `agent_display_name` field

## Testing Requirements

- **Unit tests:** Mock `AgentRepository` and `ConfigValidationHelper`. Verify: agent resolution, disabled agent rejection, display name snapshotting, default application, model re-validation.
- **Integration tests:** End-to-end: create agent via API, submit task, verify snapshot columns in DB, verify response shape.
- **Failure scenarios:** Unknown agent_id (404), disabled agent (400), agent with model that has been deactivated since creation.

## Constraints and Guardrails

- Do not reintroduce inline `agent_config` as a fallback or backward-compatibility option.
- Agent resolution and task insertion must be atomic. Do not use separate read-then-write steps without a transaction boundary or atomic SQL statement. The preferred approach is the `INSERT...SELECT` pattern described in Step 2.
- Agent config defaults (temperature, allowed_tools) must be applied at agent creation/update time (Task 2), not at submission time. The atomic INSERT snapshots config directly from the agents table.
- Task-level runtime fields (retries, max_steps, timeout, langfuse_endpoint_id) remain task-owned and are not read from the agent.

## Assumptions

- Task 2 has delivered `AgentRepository.findByIdAndTenant()` and `ConfigValidationHelper`.
- The `AgentNotFoundException` class exists from Task 2.
- `TaskRepository.insertTask()` currently has a specific parameter list that must be extended (not replaced).

<!-- AGENT_TASK_END: task-3-task-submission-refactor.md -->
