<!-- AGENT_TASK_START: task-2-api-service.md -->

# Task 2: API Service REST Endpoints

## Agent Instructions
You are a software engineer implementing one module of a larger system.
Your scope is strictly limited to this task. Do not modify components outside
the "Affected Component" listed below.

**CRITICAL PRE-WORK:** Before beginning implementation, you MUST read the following context files to understand the system architecture and constraints:
1. `docs/PROJECT.md` 
2. `docs/design/phase-1/PHASE1_DURABLE_EXECUTION.md`

**CRITICAL POST-WORK:** After completing this task, you MUST update the status of this task to "Done" in the `docs/implementation_plan/phase-1/progress.md` file.

## Context
The API Service acts as the ingest and query interface between external clients and the underlying persistence data structure holding executable tasks. It is stateless and manages input creation and state overrides via PostgreSQL transactions without LangGraph logic.

## Task-Specific Shared Contract
- Treat `docs/design/phase-1/PHASE1_DURABLE_EXECUTION.md` as the canonical API contract. Do not add endpoints, alternate state transitions, or response fields unless the design doc is updated.
- Phase 1 always resolves `tenant_id = "default"` internally, but SQL queries still remain tenant-scoped.
- `agent_config.allowed_tools` must be validated against the co-located MCP server `listTools` response at submission time.
- The API owns input validation only. It does not execute LangGraph logic, classify LLM failures, or enforce worker-side retry semantics.
- `GET /v1/tasks/{task_id}` must expose aggregate execution information derived from `checkpoints`, including `checkpoint_count` and `total_cost_microdollars`.

## Affected Component
- **Service/Module:** API Service (Java Spring Boot)
- **File paths (if known):** `services/api-service/src/main/java/` and `services/api-service/src/main/resources/`
- **Change type:** new code

## Dependencies
- **Must complete first:** Task 1 (Database Schema)
- **Provides output to:** None
- **Shared interfaces/contracts:** Exposes JSON REST constructs compatible natively with the `tasks` schema representation.

## Local Test Environment
- Reuse the existing local PostgreSQL container created by Task 1 verification for integration testing instead of provisioning a separate database.
- Expected local container: `persistent-agent-runtime-postgres`
- Expected local port mapping: `55432 -> 5432`
- Before running integration tests, use Docker to confirm the actual host port for the retained container rather than assuming it blindly. Preferred check: `docker ps --filter name=persistent-agent-runtime-postgres` or `docker port persistent-agent-runtime-postgres`.
- Expected default database settings: database `persistent_agent_runtime`, user `postgres`, password `postgres`
- Assume the Task 1 schema has already been applied in that container. If a clean reset is needed, rerun `make db-reset-verify`.
- Prefer targeting this local PostgreSQL instance for API integration tests and manual endpoint validation.

## Implementation Specification
Step 1: Implement `POST /v1/tasks` against the exact request/response contract in `docs/design/phase-1/PHASE1_DURABLE_EXECUTION.md`, inserting a `queued` task row with `tenant_id` resolved internally to `"default"` and `agent_config` persisted as `agent_config_snapshot`. The INSERT transaction must also execute `SELECT pg_notify('new_task', :worker_pool_id)` before commit, as specified in Section 5.3 of the design doc — every transition to `status='queued'` must emit NOTIFY.
Step 2: Enforce the documented API validation rules at submission time: payload size limits, supported model list, temperature range, retry/step/timeout bounds, and `allowed_tools` validation. Since the Phase 1 tool set is fixed, hardcode the allowed tool names (`web_search`, `read_url`, `calculator`) as a compile-time constant for validation — this avoids a runtime dependency on the MCP server at submission time while remaining consistent with the Task 5 `listTools` contract.
Step 3: Implement `GET /v1/tasks/{task_id}` including checkpoint count and aggregate cost derived from `checkpoints`, scoped by the internal Phase 1 tenant.
Step 4: Implement `GET /v1/tasks/{task_id}/checkpoints` returning root-namespace checkpoint history in the documented order and shape.
Step 5: Implement `POST /v1/tasks/{task_id}/cancel`, `GET /v1/tasks/dead-letter`, and `POST /v1/tasks/{task_id}/redrive` using the state transitions defined in the design doc, including tenant-scoped queries and dead-letter-only redrive. Cancel must apply to tasks in both `queued` and `running` states (see cancel query in the design doc: `WHERE status IN ('queued', 'running')`). Dead-letter listing must support `agent_id` filter and `limit` query parameters, ordered by `dead_lettered_at DESC, task_id DESC`. Redrive must also emit `pg_notify('new_task', :worker_pool_id)` since it transitions a task back to `queued`.
Step 6: Implement `GET /v1/health` returning the documented health payload, including database connectivity plus basic queue/worker counts if available.

## Acceptance Criteria
The implementation is complete when:
- [ ] All specified endpoints conform transparently against REST validation prerequisites.
- [ ] New tasks propagate reliably to the DB with strict validation filtering out unsupported models, malformed inputs, and disallowed tools.
- [ ] The API matches the Phase 1 design contract, including health, task status, checkpoint history, dead-letter listing, cancellation, and redrive behavior.

## Testing Requirements
- **Unit tests:** Extensive Controller/Service mocks validating HTTP interactions.
- **Integration tests:** REST-assured or MockMvc tests hitting the retained local PostgreSQL container (`persistent-agent-runtime-postgres` on `localhost:55432`) as the default integrated test DB environment.
- **Failure scenarios:** Test invalid DB insertions enforcing HTTP bad request status codes correctly alongside concurrency overrides.
- **Failure scenarios:** Test unsupported tools, unsupported models, out-of-range numeric limits, tenant-scoped not-found behavior, and invalid cancel/redrive transitions.

## Constraints and Guardrails
- Never evaluate worker LLM constraints inside the API boundary natively.
- No memory components directly implemented.
- Do not let the API drift from the task/checkpoint schema. If repository queries need fields not present in Task 1 output, treat that as a schema contract issue rather than introducing API-only behavior.

## Assumptions / Open Questions for This Task
- ASSUMPTION: Jackson databinding satisfies JSON payload integrations safely.

<!-- AGENT_TASK_END: task-2-api-service.md -->
