-- Phase 2 Track 4: Custom Tool Runtime (BYOT) + Task Follow-Up
-- Adds the tool_servers registry table and 'task_follow_up' event type.

-- Step 1: Create tool_servers registry table
CREATE TABLE tool_servers (
    server_id   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   TEXT NOT NULL DEFAULT 'default',
    name        TEXT NOT NULL
                CHECK (name ~ '^[a-z0-9]([a-z0-9-]*[a-z0-9])?$')
                CHECK (char_length(name) <= 100),
    url         TEXT NOT NULL
                CHECK (char_length(url) <= 2048),
    auth_type   TEXT NOT NULL DEFAULT 'none' CHECK (auth_type IN ('none', 'bearer_token')),
    auth_token  TEXT
                CHECK (auth_token IS NULL OR char_length(auth_token) <= 4096),
    status      TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'disabled')),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT chk_auth_token_consistency CHECK (
        (auth_type = 'none' AND auth_token IS NULL)
        OR (auth_type = 'bearer_token' AND auth_token IS NOT NULL)
    )
);

-- Step 2: Unique constraint — server names must be unique within a tenant
ALTER TABLE tool_servers ADD CONSTRAINT uq_tool_servers_tenant_name UNIQUE (tenant_id, name);

-- Step 3: Index for efficient lookup of active servers for a tenant
CREATE INDEX idx_tool_servers_tenant_status ON tool_servers (tenant_id, status);

-- Step 4: Table comment
COMMENT ON TABLE tool_servers IS 'Registry of external MCP tool servers. Operators register servers by URL; agents reference them by name.';

-- Step 5: Add 'task_follow_up' to the task_events event_type CHECK constraint
ALTER TABLE task_events DROP CONSTRAINT task_events_event_type_check;

ALTER TABLE task_events ADD CONSTRAINT task_events_event_type_check CHECK (event_type IN (
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
    'task_cancelled',
    'task_follow_up'
));
