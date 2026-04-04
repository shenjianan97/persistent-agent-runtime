<!-- AGENT_TASK_START: task-1-database-schema.md -->

# Task 1 — Database Schema: Agents Table

## Agent Instructions

You are a software engineer implementing one module of a larger system. Your scope is strictly limited to this task. Do not modify components outside the "Affected Component" listed below.

**CRITICAL PRE-WORK:** Before beginning implementation, you MUST read:
1. `docs/design-docs/phase-2/track-1-agent-control-plane.md` — canonical design contract (Data Model section)
2. `infrastructure/database/migrations/0001_phase1_durable_execution.sql` — existing tasks table schema

**CRITICAL POST-WORK:** After completing this task, update the status in `docs/exec-plans/completed/phase-2/track-1/progress.md` to "Done".

## Context

Track 1 of Phase 2 introduces Agent as a first-class entity. The database layer must establish the `agents` table and modify the `tasks` table to support the new snapshot column and FK constraint before any API or Console work begins.

The `agents` table stores reusable agent configuration with a composite PK `(tenant_id, agent_id)`. The `tasks` table gains an `agent_display_name_snapshot` column and a FK reference to the `agents` table.

## Task-Specific Shared Contract

- Treat `docs/design-docs/phase-2/track-1-agent-control-plane.md` as the canonical schema contract.
- Agent status values are exactly: `active`, `disabled`. No other values.
- The composite PK is `(tenant_id, agent_id)`.
- `tenant_id` remains `"default"` in Track 1 but must be part of all constraints.
- The design doc states: "Track 1 does not need to preserve existing development data." The migration assumes a clean database. No data backfill is needed.
- `agent_config` uses the same JSONB shape as the existing `agent_config_snapshot` on tasks: `system_prompt`, `provider`, `model`, `temperature`, `allowed_tools`.

## Affected Component

- **Service/Module:** Database Schema
- **File paths:**
  - `infrastructure/database/migrations/0005_agents_table.sql` (new)
  - `infrastructure/database/migrations/test_seed.sql` (modify)
- **Change type:** new migration, modification of seed file

## Dependencies

- **Must complete first:** None (entry point task)
- **Provides output to:** Task 2 (Agent CRUD API), Task 3 (Task Submission Refactor), Task 4 (Task Response Enrichment), Task 7 (Integration Tests)
- **Shared interfaces/contracts:** PostgreSQL schema contract for agent CRUD queries and task insertion

## Implementation Specification

### Step 1: Create agents table

Create `infrastructure/database/migrations/0005_agents_table.sql` with the `agents` table:

```sql
CREATE TABLE agents (
    tenant_id    TEXT NOT NULL DEFAULT 'default',
    agent_id     TEXT NOT NULL CHECK (char_length(agent_id) <= 64),
    display_name TEXT NOT NULL CHECK (char_length(display_name) <= 200),
    agent_config JSONB NOT NULL,
    status       TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'disabled')),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (tenant_id, agent_id)
);
```

### Step 2: Add index for tenant + status queries

```sql
CREATE INDEX idx_agents_tenant_status ON agents (tenant_id, status);
```

### Step 3: Add agent_display_name_snapshot to tasks

```sql
ALTER TABLE tasks ADD COLUMN agent_display_name_snapshot TEXT;
```

This column is nullable because existing tasks do not have a display name.

### Step 4: Add FK constraint

The system is still in development and the design doc states "Track 1 does not need to preserve existing development data." The migration assumes a clean database (no existing tasks referencing non-existent agents). For local development, run `make db-reset` to start fresh.

```sql
ALTER TABLE tasks ADD CONSTRAINT fk_tasks_agent
    FOREIGN KEY (tenant_id, agent_id) REFERENCES agents (tenant_id, agent_id);
```

No data backfill is needed or desired.

### Step 5: Update test seed data

Update `infrastructure/database/migrations/test_seed.sql` to insert a default agent before any seed tasks:

```sql
INSERT INTO agents (tenant_id, agent_id, display_name, agent_config, status)
VALUES ('default', 'e2e_agent', 'E2E Test Agent',
        '{"system_prompt":"You are a test assistant.","provider":"anthropic","model":"claude-sonnet-4-6","temperature":0.5,"allowed_tools":["calculator"]}'::jsonb,
        'active')
ON CONFLICT (tenant_id, agent_id) DO NOTHING;
```

Ensure any existing seed task INSERTs reference an `agent_id` that exists in the `agents` table.

## Acceptance Criteria

- [ ] `agents` table exists with correct PK `(tenant_id, agent_id)`, CHECK constraints (status values, display_name length, agent_id length), and index `idx_agents_tenant_status`
- [ ] `tasks.agent_display_name_snapshot` column exists (nullable TEXT)
- [ ] FK constraint `fk_tasks_agent` from `tasks(tenant_id, agent_id)` to `agents(tenant_id, agent_id)` is enforced
- [ ] `test_seed.sql` includes a seed agent for E2E integration tests
- [ ] All migrations 0001-0005 apply cleanly on a fresh database in sequence (via schema-bootstrap ledger)
- [ ] FK enforcement verified: INSERT into tasks with unknown agent_id fails

## Testing Requirements

- **Integration tests:** Apply all migrations in sequence on a fresh PostgreSQL container. Verify agents table DDL. Verify FK enforcement (insert task referencing nonexistent agent must fail). Verify seed data loads.
- **Failure scenarios:** FK violation on task insert with unknown `agent_id`. CHECK constraint violation for invalid status values. CHECK constraint violation for display_name exceeding 200 chars.

## Constraints and Guardrails

- Do not modify existing migration files (0001-0004). All schema changes go in `0005_agents_table.sql`.
- Do not add columns not specified in the design doc.
- Do not introduce agent statuses beyond `active` and `disabled`.

## Assumptions

- The migration runs on a clean database (no legacy task data to preserve). For local dev, `make db-reset` ensures a clean state.
- The existing `test_seed.sql` may or may not insert tasks. If it does, those tasks must reference agent_ids that exist in the agents table after the seed agent insert.
- The migration file follows the `^\d{4}_.*\.sql$` naming convention so it is picked up by both the CDK schema-bootstrap handler and the local E2E test `_apply_migrations()` function.

<!-- AGENT_TASK_END: task-1-database-schema.md -->
