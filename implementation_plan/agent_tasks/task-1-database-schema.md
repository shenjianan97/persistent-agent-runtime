<!-- AGENT_TASK_START: task-1-database-schema.md -->

# Task 1: Database Schema

## Agent Instructions
You are a software engineer implementing one module of a larger system.
Your scope is strictly limited to this task. Do not modify components outside
the "Affected Component" listed below.

**CRITICAL PRE-WORK:** Before beginning implementation, you MUST read the following context files to understand the system architecture and constraints:
1. `PROJECT.md` 
2. `design/PHASE1_DURABLE_EXECUTION.md`

**CRITICAL POST-WORK:** After completing this task, you MUST update the status of this task to "Done" in the `implementation_plan/progress.md` file.

## Context
Phase 1 of the Persistent Agent Runtime relies on a Database-as-a-Queue model to eliminate dual-write hazards. We use PostgreSQL to serialize durable graph engine state (via checkpoints) alongside a `tasks` queue architecture, managed comprehensively by lease-ownership paradigms and structured tables. Refer to PHASE1_DURABLE_EXECUTION.md under Database Schema & Key Queries (Section 6.1) for complete design constructs.

## Task-Specific Shared Contract
- Treat `design/PHASE1_DURABLE_EXECUTION.md` as the canonical schema contract. Do not invent additional statuses, dead-letter reasons, or retry semantics.
- `tasks.status` values are exactly: `queued`, `running`, `completed`, `dead_letter`.
- `dead_letter_reason` values are exactly: `cancelled_by_user`, `retries_exhausted`, `task_timeout`, `non_retryable_error`, `max_steps_exceeded`.
- Phase 1 API reads are tenant-scoped with `tenant_id = "default"`, so the schema must retain `tenant_id` even though Phase 1 is single-tenant in practice.
- The schema must support these query families exactly as designed: claim via `FOR UPDATE SKIP LOCKED`, heartbeat lease extension, lease-aware checkpoint writes, retry requeue with `retry_after`, reaper reclaim, timeout dead-lettering, cancel, and redrive.

## Affected Component
- **Service/Module:** Database Schema Initialization
- **File paths (if known):** `infrastructure/database/`
- **Change type:** new code

## Dependencies
- **Must complete first:** None
- **Provides output to:** Task 2, Task 3, Task 4
- **Shared interfaces/contracts:** Standard PostgreSQL schema layout constraints.

## Implementation Specification
Step 1: Implement the `tasks` table exactly as specified in `design/PHASE1_DURABLE_EXECUTION.md`, including `tenant_id`, `agent_id`, `agent_config_snapshot`, `status`, `worker_pool_id`, `version`, `input`, `output`, `lease_owner`, `lease_expiry`, `retry_count`, `max_retries`, `retry_after`, `retry_history`, `task_timeout_seconds`, `max_steps`, `last_error_code`, `last_error_message`, `last_worker_id`, `dead_letter_reason`, `dead_lettered_at`, `created_at`, and `updated_at`. Note: `updated_at` uses `DEFAULT NOW()` for INSERT only. All application UPDATE queries must explicitly set `updated_at = NOW()` — the design doc's key queries all follow this pattern. Do not rely on the DEFAULT for updates.
Step 2: Create the required Phase 1 indexes supporting claim, reaper, timeout, tenant/agent lookup, and dead-letter listing: `idx_tasks_claim`, `idx_tasks_lease_expiry`, `idx_tasks_timeout`, `idx_tasks_tenant_agent`, and `idx_tasks_dead_letter`.
Step 3: Define the `checkpoints` table with the exact composite primary key and columns from the design doc, including `worker_id`, `parent_checkpoint_id`, `thread_ts`, `parent_ts`, `checkpoint_payload`, `metadata_payload`, `cost_microdollars`, `execution_metadata`, and `created_at`, plus the required checkpoint lookup indexes: `idx_checkpoints_task_ts` and `idx_checkpoints_task_created`.
Step 4: Define the `checkpoint_writes` table exactly to support `BaseCheckpointSaver.put_writes()`, including the composite primary key and composite foreign key back to `checkpoints`.
Step 5: Document in schema README that `LISTEN/NOTIFY` is application-level, not schema-level. There are no database triggers or functions to create for NOTIFY. Application code (API Service and Worker Service) must call `pg_notify('new_task', worker_pool_id)` inline within transactions that transition tasks to `status='queued'` (submission, retry requeue, reaper reclaim, redrive), as shown in the design doc's key queries.
Step 6: Include migration and verification coverage for the exact key query patterns in the design doc: claim, heartbeat, lease-aware checkpoint writes, retry requeue, reaper reclaim, timeout dead-lettering, cancellation, and redrive.

## Acceptance Criteria
The implementation is complete when:
- [ ] Primary queue and telemetry tables exist efficiently without cyclic dependency.
- [ ] Schema applies successfully matching the exact Phase 1 architecture doc without runtime schema errors.
- [ ] `FOR UPDATE SKIP LOCKED`, retry backoff, dead-letter filtering, and reaper scans are all supported by the shipped indexes and constraints.
- [ ] The schema supports all documented API and worker transitions without adding undocumented columns or omitting required ones.

## Testing Requirements
- **Unit tests:** Not directly applicable to raw DB definitions.
- **Integration tests:** Spin up a PostgreSQL test container attempting schema migration checks validating table completeness securely.
- **Failure scenarios:** Verify constraints trigger appropriately on malformed inserts (e.g., missing agent config, impossible status entries).

## Constraints and Guardrails
- Utilize isolated tenant keys allowing seamless future auth upgrades.
- Must execute deterministically.
- Do not add undocumented columns to compensate for application logic uncertainty; if a required field is missing, the design doc should be updated first.

## Assumptions / Open Questions for This Task
- None

<!-- AGENT_TASK_END: task-1-database-schema.md -->
