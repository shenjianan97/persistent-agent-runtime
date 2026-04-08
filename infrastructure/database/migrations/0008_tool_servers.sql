-- Phase 2 Track 4: Custom Tool Runtime (BYOT)
-- Adds the tool_servers registry table for external MCP tool server registrations.

-- Step 1: Create tool_servers registry table
CREATE TABLE tool_servers (
    server_id   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   TEXT NOT NULL DEFAULT 'default',
    name        TEXT NOT NULL CHECK (name ~ '^[a-z0-9][a-z0-9-]*$'),
    url         TEXT NOT NULL,
    auth_type   TEXT NOT NULL DEFAULT 'none' CHECK (auth_type IN ('none', 'bearer_token')),
    auth_token  TEXT,
    status      TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'disabled')),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Step 2: Unique constraint — server names must be unique within a tenant
ALTER TABLE tool_servers ADD CONSTRAINT uq_tool_servers_tenant_name UNIQUE (tenant_id, name);

-- Step 3: Index for efficient lookup of active servers for a tenant
CREATE INDEX idx_tool_servers_tenant_status ON tool_servers (tenant_id, status);

-- Step 4: Table comment
COMMENT ON TABLE tool_servers IS 'Registry of external MCP tool servers. Operators register servers by URL; agents reference them by name.';
