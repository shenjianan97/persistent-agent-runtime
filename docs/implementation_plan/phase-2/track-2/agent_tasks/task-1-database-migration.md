<!-- AGENT_TASK_START: task-1-database-migration.md -->

# Task 1 — Database Migration: Runtime State Model Schema

## Agent Instructions

You are a software engineer implementing one module of a larger system. Your scope is strictly limited to this task. Do not modify components outside the "Affected Component" listed below.

**CRITICAL PRE-WORK:** Before beginning implementation, you MUST read:
1. `docs/design/phase-2/PHASE2_MULTI_AGENT.md` — canonical design contract (Sections 5, 7, 8)
2. `infrastructure/database/migrations/0001_phase1_durable_execution.sql` — existing tasks table schema and CHECK constraints
3. `infrastructure/database/migrations/0005_agents_table.sql` — Track 1 migration pattern

**CRITICAL POST-WORK:** After completing this task, update the status in `docs/implementation_plan/phase-2/track-2/progress.md` to "Done".

## Context

Track 2 of Phase 2 extends the task lifecycle with durable pause states, an append-only event history table, and new task columns for human-in-the-loop workflows. The database layer must establish these foundations before any API, worker, or Console work begins.

The existing task status CHECK constraint must be expanded to include `waiting_for_approval`, `waiting_for_input`, and `paused`. A new `task_events` table provides the append-only audit trail. New columns on `tasks` support the HITL workflow state.

## Task-Specific Shared Contract

- Treat `docs/design/phase-2/PHASE2_MULTI_AGENT.md` Sections 5, 7, 8 as the canonical schema contract.
- The three new statuses are: `waiting_for_approval`, `waiting_for_input`, `paused`.
- The `paused` status is added now but not implemented until Track 3 (budget enforcement).
- New dead letter reasons: `human_input_timeout`, `rejected_by_user`.
- All new columns on `tasks` are nullable — existing tasks are unaffected.
- The `task_events` table is append-only. Events are never updated or deleted.
- Event types follow the design doc Section 5 entity definition, extended with HITL-specific types.

## Affected Component

- **Service/Module:** Database Schema
- **File paths:**
  - `infrastructure/database/migrations/0006_runtime_state_model.sql` (new)
- **Change type:** new migration

## Dependencies

- **Must complete first:** None (entry point task)
- **Provides output to:** Task 2 (Event Service), Task 3 (HITL API), Task 4 (Worker Interrupt), Task 5 (Event Integration), Task 6 (Console Updates), Task 7 (Integration Tests)
- **Shared interfaces/contracts:** PostgreSQL schema contract for task status transitions, event recording, and HITL state

## Implementation Specification

### Step 1: Expand tasks status CHECK constraint

The current constraint in `0001_phase1_durable_execution.sql` is:
```sql
CHECK (status IN ('queued', 'running', 'completed', 'dead_letter'))
```

Drop and recreate it:
```sql
ALTER TABLE tasks DROP CONSTRAINT tasks_status_check;
ALTER TABLE tasks ADD CONSTRAINT tasks_status_check
    CHECK (status IN ('queued', 'running', 'completed', 'dead_letter',
                      'waiting_for_approval', 'waiting_for_input', 'paused'));
```

### Step 2: Expand dead_letter_reason CHECK constraint

The current constraint allows: `cancelled_by_user`, `retries_exhausted`, `task_timeout`, `non_retryable_error`, `max_steps_exceeded`.

Drop and recreate to add `human_input_timeout` and `rejected_by_user`:
```sql
ALTER TABLE tasks DROP CONSTRAINT tasks_dead_letter_reason_check;
ALTER TABLE tasks ADD CONSTRAINT tasks_dead_letter_reason_check
    CHECK (dead_letter_reason IN ('cancelled_by_user', 'retries_exhausted', 'task_timeout',
                                   'non_retryable_error', 'max_steps_exceeded',
                                   'human_input_timeout', 'rejected_by_user'));
```

### Step 3: Add new columns to tasks

```sql
ALTER TABLE tasks ADD COLUMN pending_input_prompt TEXT;
ALTER TABLE tasks ADD COLUMN pending_approval_action JSONB;
ALTER TABLE tasks ADD COLUMN human_input_timeout_at TIMESTAMPTZ;
ALTER TABLE tasks ADD COLUMN human_response TEXT;
```

- `pending_input_prompt`: What the agent is asking the human (set when entering `waiting_for_input`)
- `pending_approval_action`: The tool call awaiting approval as JSONB (set when entering `waiting_for_approval`)
- `human_input_timeout_at`: Deadline for human response; reaper dead-letters past this time
- `human_response`: Stores the human's approval/rejection reason or freeform input for pickup on resume

### Step 4: Create task_events table

```sql
CREATE TABLE task_events (
    event_id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id      TEXT NOT NULL,
    task_id        UUID NOT NULL REFERENCES tasks(task_id),
    agent_id       TEXT NOT NULL,
    event_type     TEXT NOT NULL CHECK (event_type IN (
        'task_submitted',
        'task_claimed',
        'task_retry_scheduled',
        'task_reclaimed_after_lease_expiry',
        'task_dead_lettered',
        'task_redriven',
        'task_completed',
        'task_paused',
        'task_resumed',
        'task_approval_requested',
        'task_approved',
        'task_rejected',
        'task_input_requested',
        'task_input_received',
        'task_cancelled'
    )),
    status_before  TEXT,
    status_after   TEXT,
    worker_id      TEXT,
    error_code     TEXT,
    error_message  TEXT,
    details        JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

### Step 5: Create indexes

```sql
-- Primary lookup: events for a specific task in chronological order
CREATE INDEX idx_task_events_task ON task_events (task_id, created_at);

-- Tenant-scoped listing (e.g., recent events across all tasks)
CREATE INDEX idx_task_events_tenant ON task_events (tenant_id, created_at DESC);

-- Reaper scan for human-input-timeout (partial index for efficiency)
CREATE INDEX idx_tasks_human_input_timeout ON tasks (human_input_timeout_at)
    WHERE human_input_timeout_at IS NOT NULL
      AND status IN ('waiting_for_approval', 'waiting_for_input');
```

## Acceptance Criteria

- [ ] Migration `0006_runtime_state_model.sql` applies cleanly on a fresh database after migrations 0001-0005
- [ ] Tasks table accepts the three new status values (`waiting_for_approval`, `waiting_for_input`, `paused`)
- [ ] Tasks table rejects invalid status values (CHECK constraint enforced)
- [ ] Dead letter reason CHECK accepts `human_input_timeout` and `rejected_by_user`
- [ ] New nullable columns exist: `pending_input_prompt`, `pending_approval_action`, `human_input_timeout_at`, `human_response`
- [ ] `task_events` table is writable with valid event types
- [ ] `task_events` table rejects invalid event types (CHECK constraint enforced)
- [ ] All indexes created: `idx_task_events_task`, `idx_task_events_tenant`, `idx_tasks_human_input_timeout`
- [ ] Existing test seeds still load successfully
- [ ] `make db-reset` completes without errors

## Testing Requirements

- **Integration tests:** Apply all migrations 0001-0006 in sequence on a fresh PostgreSQL container. Verify new status values accepted. Verify CHECK constraint enforcement. Verify task_events INSERT/SELECT. Verify indexes exist.
- **Failure scenarios:** INSERT task with invalid status value must fail. INSERT task_event with invalid event_type must fail.

## Constraints and Guardrails

- Do not modify existing migration files (0001-0005). All schema changes go in `0006_runtime_state_model.sql`.
- Do not add columns not specified in this task.
- Do not implement any application-level logic — this task is schema-only.
- The `paused` status is added to the enum but no application code should use it yet (Track 3).

## Assumptions

- The migration runs after `0005_agents_table.sql` (Track 1) has been applied.
- The naming convention `^\d{4}_.*\.sql$` is followed for automatic pickup by the schema-bootstrap ledger.
- No data backfill is needed — new columns are nullable and new tables are empty.

<!-- AGENT_TASK_END: task-1-database-migration.md -->
