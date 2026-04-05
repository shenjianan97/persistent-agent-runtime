-- Phase 2 Track 3: Scheduler and Budgets
-- Adds agent concurrency/budget columns, task pause metadata,
-- scheduler state tables, cost ledger, and supporting indexes.

-- Step 1: Add budget/concurrency columns to agents table
ALTER TABLE agents ADD COLUMN max_concurrent_tasks INT NOT NULL DEFAULT 5 CHECK (max_concurrent_tasks > 0);
ALTER TABLE agents ADD COLUMN budget_max_per_task BIGINT NOT NULL DEFAULT 500000 CHECK (budget_max_per_task > 0);
ALTER TABLE agents ADD COLUMN budget_max_per_hour BIGINT NOT NULL DEFAULT 5000000 CHECK (budget_max_per_hour > 0);

-- Step 2: Add pause metadata columns to tasks table
ALTER TABLE tasks ADD COLUMN pause_reason TEXT;
ALTER TABLE tasks ADD COLUMN pause_details JSONB;
ALTER TABLE tasks ADD COLUMN resume_eligible_at TIMESTAMPTZ;

-- Step 3: Create agent_runtime_state table
CREATE TABLE agent_runtime_state (
    tenant_id             TEXT NOT NULL,
    agent_id              TEXT NOT NULL,
    running_task_count    INT NOT NULL DEFAULT 0,
    hour_window_cost_microdollars BIGINT NOT NULL DEFAULT 0,
    scheduler_cursor      TIMESTAMPTZ NOT NULL DEFAULT '1970-01-01T00:00:00Z',
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (tenant_id, agent_id)
);

-- Step 4: Create agent_cost_ledger table
CREATE TABLE agent_cost_ledger (
    entry_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       TEXT NOT NULL,
    agent_id        TEXT NOT NULL,
    task_id         UUID NOT NULL,
    checkpoint_id   TEXT NOT NULL,
    cost_microdollars BIGINT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Step 5: Create indexes
CREATE INDEX idx_agent_cost_ledger_window ON agent_cost_ledger (tenant_id, agent_id, created_at);
CREATE INDEX idx_agent_cost_ledger_task ON agent_cost_ledger (task_id);
CREATE INDEX idx_tasks_budget_resume ON tasks (resume_eligible_at)
    WHERE resume_eligible_at IS NOT NULL
      AND status = 'paused'
      AND pause_reason = 'budget_per_hour';

-- Step 6: Seed agent_runtime_state for existing agents
INSERT INTO agent_runtime_state (tenant_id, agent_id, running_task_count, hour_window_cost_microdollars, scheduler_cursor, updated_at)
SELECT a.tenant_id, a.agent_id,
       COALESCE((SELECT COUNT(*) FROM tasks t WHERE t.tenant_id = a.tenant_id AND t.agent_id = a.agent_id AND t.status = 'running'), 0),
       0, '1970-01-01T00:00:00Z'::timestamptz, NOW()
FROM agents a
ON CONFLICT DO NOTHING;
