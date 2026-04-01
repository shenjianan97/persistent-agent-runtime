-- Phase 2 Track 1: Agents as a first-class entity
-- Adds the agents table and links tasks to agents via FK.

-- Step 1: Create agents table
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

-- Step 2: Index for tenant + status queries
CREATE INDEX idx_agents_tenant_status ON agents (tenant_id, status);

-- Step 3: Add agent_display_name_snapshot to tasks
ALTER TABLE tasks ADD COLUMN agent_display_name_snapshot TEXT;

-- Step 4: FK constraint from tasks to agents
ALTER TABLE tasks ADD CONSTRAINT fk_tasks_agent
    FOREIGN KEY (tenant_id, agent_id) REFERENCES agents (tenant_id, agent_id);
