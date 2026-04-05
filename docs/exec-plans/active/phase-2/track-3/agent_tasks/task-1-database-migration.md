<!-- AGENT_TASK_START: task-1-database-migration.md -->

# Task 1 — Database Migration: Scheduler and Budget Schema

## Agent Instructions

You are a software engineer implementing one module of a larger system. Your scope is strictly limited to this task. Do not modify components outside the "Affected Component" listed below.

**CRITICAL PRE-WORK:** Before beginning implementation, you MUST read:
1. `docs/design-docs/phase-2/track-3-scheduler-and-budgets.md` — canonical design contract (Data Model section)
2. `infrastructure/database/migrations/0005_agents_table.sql` — existing agents table schema
3. `infrastructure/database/migrations/0006_runtime_state_model.sql` — Track 2 migration (task_events, HITL columns, status expansion)
4. `infrastructure/database/migrations/0001_phase1_durable_execution.sql` — tasks table schema and CHECK constraints

**CRITICAL POST-WORK:** After completing this task:
1. Run the full test suite with `make test` and verify all existing tests still pass. Fix any regressions before proceeding.
2. Update the status in `docs/exec-plans/active/phase-2/track-3/progress.md` to "Done".

## Context

Track 3 introduces agent-aware scheduling with concurrency limits, budget enforcement, and fair scheduling. The database layer must establish the schema foundations before any worker, API, or Console work begins.

This migration adds:
- Budget and concurrency columns to the `agents` table
- Pause metadata columns to the `tasks` table
- A derived `agent_runtime_state` table for cheap claim-time eligibility checks
- An append-only `agent_cost_ledger` table for rolling hourly spend tracking

## Task-Specific Shared Contract

- Treat `docs/design-docs/phase-2/track-3-scheduler-and-budgets.md` Data Model section as the canonical schema contract.
- All budget values are stored in microdollars (1 USD = 1,000,000 microdollars).
- `agent_runtime_state` is derived operational state, not the source of truth for task history.
- `agent_cost_ledger` is the canonical source for rolling hourly spend.
- The migration must seed `agent_runtime_state` rows for all existing agents.
- New agent columns have NOT NULL constraints with sensible defaults — existing agents are unaffected.
- New task columns are nullable — existing tasks are unaffected.
- The `task_events` event_type CHECK constraint must be left unchanged — `task_paused` and `task_resumed` already exist from Track 2.

## Affected Component

- **Service/Module:** Database Schema
- **File paths:**
  - `infrastructure/database/migrations/0007_scheduler_and_budgets.sql` (new)
- **Change type:** new migration

## Dependencies

- **Must complete first:** None (entry point task)
- **Provides output to:** Task 2 (Incremental Cost), Task 3 (Scheduler Claim), Task 4 (Budget Enforcement), Task 5 (Reaper Recovery), Task 6 (API Extensions), Task 7 (Console Updates), Task 8 (Integration Tests)
- **Shared interfaces/contracts:** PostgreSQL schema contract for scheduler state, cost ledger, budget columns, and pause metadata

## Implementation Specification

### Step 1: Add budget and concurrency columns to agents table

```sql
ALTER TABLE agents ADD COLUMN max_concurrent_tasks INT NOT NULL DEFAULT 5 CHECK (max_concurrent_tasks > 0);
ALTER TABLE agents ADD COLUMN budget_max_per_task BIGINT NOT NULL DEFAULT 500000 CHECK (budget_max_per_task > 0);
ALTER TABLE agents ADD COLUMN budget_max_per_hour BIGINT NOT NULL DEFAULT 5000000 CHECK (budget_max_per_hour > 0);
```

- `max_concurrent_tasks`: global running-task cap for the agent (default 5)
- `budget_max_per_task`: per-task budget in microdollars (default $0.50)
- `budget_max_per_hour`: rolling hourly budget in microdollars (default $5.00)

### Step 2: Add pause metadata columns to tasks table

```sql
ALTER TABLE tasks ADD COLUMN pause_reason TEXT;
ALTER TABLE tasks ADD COLUMN pause_details JSONB;
ALTER TABLE tasks ADD COLUMN resume_eligible_at TIMESTAMPTZ;
```

- `pause_reason`: `budget_per_task` or `budget_per_hour` in Track 3 (nullable, null for non-paused tasks)
- `pause_details`: structured budget context for API and UI surfacing
- `resume_eligible_at`: next known auto-resume time for hourly budget pauses

### Step 3: Create agent_runtime_state table

```sql
CREATE TABLE agent_runtime_state (
    tenant_id             TEXT NOT NULL,
    agent_id              TEXT NOT NULL,
    running_task_count    INT NOT NULL DEFAULT 0,
    hour_window_cost_microdollars BIGINT NOT NULL DEFAULT 0,
    scheduler_cursor      TIMESTAMPTZ NOT NULL DEFAULT '1970-01-01T00:00:00Z',
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (tenant_id, agent_id)
);
```

One row per `(tenant_id, agent_id)`. This is derived operational state used by the scheduler for cheap eligibility checks.

### Step 4: Create agent_cost_ledger table

```sql
CREATE TABLE agent_cost_ledger (
    entry_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       TEXT NOT NULL,
    agent_id        TEXT NOT NULL,
    task_id         UUID NOT NULL,
    checkpoint_id   TEXT NOT NULL,
    cost_microdollars BIGINT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

Append-only cost entries for rolling hourly spend calculation.

### Step 5: Create indexes

```sql
-- Rolling-window queries on cost ledger during claim-time budget checks and reaper scans
CREATE INDEX idx_agent_cost_ledger_window ON agent_cost_ledger (tenant_id, agent_id, created_at);

-- Per-task cost aggregation from ledger
CREATE INDEX idx_agent_cost_ledger_task ON agent_cost_ledger (task_id);

-- Reaper scan for hourly budget auto-recovery (partial index for paused tasks with resume_eligible_at)
CREATE INDEX idx_tasks_budget_resume ON tasks (resume_eligible_at)
    WHERE resume_eligible_at IS NOT NULL
      AND status = 'paused'
      AND pause_reason = 'budget_per_hour';
```

### Step 6: Seed agent_runtime_state for existing agents

```sql
INSERT INTO agent_runtime_state (tenant_id, agent_id, running_task_count, hour_window_cost_microdollars, scheduler_cursor, updated_at)
SELECT a.tenant_id, a.agent_id,
       COALESCE((SELECT COUNT(*) FROM tasks t WHERE t.tenant_id = a.tenant_id AND t.agent_id = a.agent_id AND t.status = 'running'), 0),
       0, '1970-01-01T00:00:00Z'::timestamptz, NOW()
FROM agents a
ON CONFLICT DO NOTHING;
```

This seeds runtime state for all agents that existed before Track 3. The running_task_count is initialized from the actual count of currently running tasks. The hourly cost cache starts at 0 (the reaper will recompute it from the ledger on its first cycle).

## Acceptance Criteria

- [ ] Migration `0007_scheduler_and_budgets.sql` applies cleanly on a fresh database after migrations 0001-0006
- [ ] Agents table has `max_concurrent_tasks` (INT, NOT NULL, default 5, CHECK > 0)
- [ ] Agents table has `budget_max_per_task` (BIGINT, NOT NULL, default 500000, CHECK > 0)
- [ ] Agents table has `budget_max_per_hour` (BIGINT, NOT NULL, default 5000000, CHECK > 0)
- [ ] Tasks table has nullable `pause_reason`, `pause_details`, `resume_eligible_at` columns
- [ ] `agent_runtime_state` table exists with correct primary key and columns
- [ ] `agent_cost_ledger` table exists with correct primary key and columns
- [ ] Indexes `idx_agent_cost_ledger_window`, `idx_agent_cost_ledger_task`, `idx_tasks_budget_resume` are created
- [ ] Existing agents have `agent_runtime_state` rows seeded via migration
- [ ] Existing test seeds still load successfully
- [ ] `make db-reset` completes without errors

## Testing Requirements

- **Integration tests:** Apply all migrations 0001-0007 in sequence on a fresh PostgreSQL container. Verify new agent columns exist with correct defaults and CHECK constraints. Verify new task columns are nullable. Verify `agent_runtime_state` and `agent_cost_ledger` tables are writable. Verify indexes exist.
- **Failure scenarios:** INSERT agent with `max_concurrent_tasks = 0` must fail. INSERT agent with `budget_max_per_task = -1` must fail. INSERT agent with `budget_max_per_hour = 0` must fail.

## Constraints and Guardrails

- Do not modify existing migration files (0001-0006). All schema changes go in `0007_scheduler_and_budgets.sql`.
- Do not add columns not specified in this task.
- Do not implement any application-level logic — this task is schema-only.
- Do not modify the `task_events` event_type CHECK constraint — `task_paused` and `task_resumed` already exist.

## Assumptions

- The migration runs after `0006_runtime_state_model.sql` (Track 2) has been applied.
- The naming convention `^\d{4}_.*\.sql$` is followed for automatic pickup by the schema-bootstrap ledger.
- Existing agents get sensible defaults for budget/concurrency columns (no manual backfill needed).
- Use `-- Step N:` comment headers in the migration file to match the convention in `0005` and `0006`.
- **Note for Task 2:** The existing `checkpoints.cost_microdollars` column is `INT` (not `BIGINT`). The `agent_cost_ledger.cost_microdollars` column is `BIGINT`. This is intentional — individual checkpoint costs are unlikely to exceed INT range, while cumulative budget values may.

<!-- AGENT_TASK_END: task-1-database-migration.md -->
