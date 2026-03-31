<!-- AGENT_TASK_START: task-4-api-observability-refactor.md -->

# Task 4: API Observability Refactor — Checkpoint-Based Costs

## Agent Instructions
You are a software engineer implementing one module of a larger system.
Your scope is strictly limited to this task. Do not modify components outside
the "Affected Component" listed below.

**CRITICAL PRE-WORK:** Before beginning implementation, you MUST read the following context files:
1. `docs/design/langfuse-customer-integration/design.md`
2. `services/api-service/src/main/java/com/persistentagent/api/service/observability/LangfuseTaskObservabilityService.java` (to be removed)
3. `services/api-service/src/main/java/com/persistentagent/api/service/observability/TaskObservabilityService.java` (interface to simplify)
4. `services/api-service/src/main/java/com/persistentagent/api/service/TaskService.java` (observability integration points)
5. `services/api-service/src/main/java/com/persistentagent/api/controller/TaskController.java` (observability endpoint)
6. `services/api-service/src/main/java/com/persistentagent/api/repository/TaskRepository.java` (checkpoint queries)
7. `services/api-service/src/main/resources/application.yml` (Langfuse config to remove)

## Context
The API service currently queries the customer's Langfuse instance to serve cost/trace data — this is architecturally wrong. The platform should not access customer's external services. With Task 3 restoring internal cost tracking to checkpoint rows, the API service can source all cost/token data from its own database. This task removes the Langfuse query code and rewires observability to use checkpoint aggregation.

## Task-Specific Shared Contract
- The platform NEVER queries a customer's Langfuse instance. Langfuse is write-only from the worker's perspective.
- Cost/token data comes from checkpoint `cost_microdollars` (INT) and `execution_metadata` (JSONB) columns.
- The observability endpoint returns platform-owned data only: checkpoint events, cost per step, retry markers, completion status.
- The response shape changes (Langfuse spans removed) — this is a breaking change to the observability endpoint.

## Affected Component
- **Service/Module:** API Service — Observability
- **File paths:** `services/api-service/src/main/java/com/persistentagent/api/service/observability/`, `services/api-service/src/main/java/com/persistentagent/api/service/TaskService.java`, `services/api-service/src/main/java/com/persistentagent/api/controller/TaskController.java`, `services/api-service/src/main/resources/application.yml`
- **Change type:** removal + modification

## Dependencies
- **Must complete first:** Task 2 (endpoint repository available), Task 3 (cost data in checkpoints)
- **Provides output to:** Task 5
- **Shared interfaces/contracts:** Simplified observability response consumed by Console.

## Implementation Specification

### Step 1: Remove Langfuse Query Code

Delete the following files entirely:
- `LangfuseTaskObservabilityService.java` (462 lines) — queries customer's Langfuse REST API
- `TaskObservabilityTotals.java` — Langfuse-specific totals record

### Step 2: Simplify Observability Interface

Modify `TaskObservabilityService.java`:
- Rename to `CheckpointObservabilityService.java` (or keep the name but change the contract)
- Replace the two methods with checkpoint-based equivalents:
  - `getTaskCostTotals(UUID taskId) -> TaskCostTotals` — aggregates cost from checkpoints
  - `getTaskExecutionTimeline(UUID taskId, String agentId, String taskStatus) -> TaskObservabilityResponse` — returns checkpoint events and runtime markers

Create a new implementation `CheckpointObservabilityServiceImpl.java`:

**`getTaskCostTotals(taskId)`:**
- Call `TaskRepository.getCheckpoints(taskId, tenantId)` to fetch all root-namespace checkpoints
- For each checkpoint row:
  - Sum `cost_microdollars` (INT column, already present)
  - Parse `execution_metadata` (JSONB) to extract `input_tokens`, `output_tokens`, `model`
  - Accumulate totals: `total_cost`, `total_input_tokens`, `total_output_tokens`
- If `execution_metadata` is null or malformed for a checkpoint, treat its tokens as 0 (the cost_microdollars column is the authoritative cost source)
- Return `TaskCostTotals(totalCostMicrodollars, inputTokens, outputTokens, totalTokens, durationMs)`
- `durationMs`: calculate from first checkpoint's `created_at` to last checkpoint's `created_at`, or null if < 2 checkpoints

**`getTaskExecutionTimeline(taskId, agentId, taskStatus)`:**
- Fetch checkpoints via `TaskRepository.getCheckpoints()`
- Build one `TaskObservabilityItemResponse` per checkpoint:
  - `kind`: `checkpoint_persisted`
  - `step_number`: sequential (1, 2, 3...)
  - `cost_microdollars`: from checkpoint row
  - `input_tokens`, `output_tokens`: from `execution_metadata` JSONB
  - `title`: `"Checkpoint {N}"` or model name from `execution_metadata.model` if present
  - `started_at`: checkpoint's `created_at`
- Append runtime markers based on task status:
  - If task has retry history → `resumed_after_retry` items with timestamps from `retry_history` JSONB array
  - If `status = 'completed'` → final `completed` item at `updated_at`
  - If `status = 'dead_letter'` → final `dead_lettered` item at `dead_lettered_at`
- Sort all items by timestamp ascending
- Wrap in `TaskObservabilityResponse` with aggregated totals

**Note:** `TaskRepository.getCheckpoints()` already exists and returns `checkpoint_id, worker_id, cost_microdollars, execution_metadata, metadata_payload, checkpoint_payload, created_at` ordered by `created_at ASC`.

### Step 3: Modify TaskService

Modify `TaskService.java`:

In `getTaskStatus()`:
- Replace `taskObservabilityService.getTaskTotals()` with `checkpointObservabilityService.getTaskCostTotals()`
- Cost totals now come from SUM of checkpoint `cost_microdollars`

In `getTaskObservability()`:
- Replace Langfuse span fetching with checkpoint-based timeline
- Keep the existing runtime item generation (checkpoint markers, retry events, completion/dead-letter markers)
- Remove the Langfuse span merging logic
- Items are checkpoint events with cost data, ordered by `created_at`

### Step 4: Simplify Response Models

Modify `TaskObservabilityResponse.java`:
- Remove `spans` field (Langfuse spans)
- Remove `trace_id` field (Langfuse trace ID)
- Keep: `enabled` (always true — platform data is always available), `task_id`, `agent_id`, `status`, `total_cost_microdollars`, `input_tokens`, `output_tokens`, `total_tokens`, `duration_ms`, `items`

Delete `TaskObservabilitySpanResponse.java` — these were Langfuse spans, no longer needed.

Simplify `TaskObservabilityItemResponse.java`:
- Remove Langfuse-specific `kind` values (`llm_span`, `tool_span`, `system_span`)
- Keep checkpoint/runtime kinds: `checkpoint_persisted`, `resumed_after_retry`, `completed`, `dead_lettered`
- Each checkpoint item includes `cost_microdollars`, `input_tokens`, `output_tokens` from `execution_metadata`

### Step 5: Remove Global Langfuse Config

Modify `application.yml`:
- Remove the entire `app.langfuse` block:
  ```yaml
  # REMOVE:
  app:
    langfuse:
      enabled: ...
      host: ...
      public-key: ...
      secret-key: ...
  ```

### Step 6: Update TaskController

Modify `TaskController.java`:
- The `GET /{taskId}/observability` endpoint stays but returns the simplified response
- No changes to the endpoint contract (same path, same HTTP method)
- Response body shape changes (no spans, no trace_id)

## Acceptance Criteria
- [ ] `LangfuseTaskObservabilityService.java` is deleted.
- [ ] `TaskObservabilityTotals.java` is deleted.
- [ ] `TaskObservabilitySpanResponse.java` is deleted.
- [ ] `application.yml` has no `app.langfuse` configuration.
- [ ] No imports of `langfuse` or references to Langfuse host/credentials in the API service.
- [ ] `GET /v1/tasks/{taskId}/observability` returns checkpoint-based cost and timeline data.
- [ ] `GET /v1/tasks/{taskId}` includes cost totals from checkpoint aggregation.
- [ ] Cost data matches what the worker wrote to checkpoint rows.
- [ ] API service starts successfully without any Langfuse environment variables.

## Testing Requirements
- **Unit tests:** Test the new checkpoint-based observability service with mocked checkpoint data. Test cost aggregation logic.
- **Integration tests:** Submit a task, let it complete, verify observability endpoint returns correct checkpoint-sourced costs and timeline.
- **Failure scenarios:** Task with no checkpoints (should return zero costs). Task in progress (should return partial data).

## Constraints and Guardrails
- The API service must NOT make any HTTP calls to external Langfuse instances.
- Remove all Langfuse-related dependencies from the API service if any were added (check `build.gradle` or `pom.xml`).
- The `enabled` field in the response should always be `true` — platform cost data is always available regardless of Langfuse configuration.
- Do not break the Console — the response shape change will be handled in Task 5.
