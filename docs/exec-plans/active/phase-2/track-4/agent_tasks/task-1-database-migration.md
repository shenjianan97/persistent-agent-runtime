<!-- AGENT_TASK_START: task-1-database-migration.md -->

# Task 1 — Database Migration: Tool Servers Registry

## Agent Instructions

You are a software engineer implementing one module of a larger system. Your scope is strictly limited to this task. Do not modify components outside the "Affected Component" listed below.

**CRITICAL PRE-WORK:** Before beginning implementation, you MUST read:
1. `docs/design-docs/phase-2/track-4-custom-tool-runtime.md` — canonical design contract (Data Model section)
2. `infrastructure/database/migrations/0007_scheduler_and_budgets.sql` — Track 3 migration (latest existing migration)
3. `infrastructure/database/migrations/0005_agents_table.sql` — agents table schema
4. `infrastructure/database/migrations/0001_phase1_durable_execution.sql` — tasks table schema and conventions

**CRITICAL POST-WORK:** After completing this task:
1. Run the full test suite with `make test` and verify all existing tests still pass. Fix any regressions before proceeding.
2. Update the status in `docs/exec-plans/active/phase-2/track-4/progress.md` to "Done".

## Context

Track 4 introduces custom tool server support (BYOT — Bring Your Own Tools). Operators register external MCP tool servers by HTTP URL, and agents reference them in their configuration. The database layer must establish the `tool_servers` registry table before any API, worker, or Console work begins.

This migration adds:
- A `tool_servers` table for registering external MCP server endpoints
- Unique constraint on `(tenant_id, name)` to prevent duplicate registrations
- Index on `(tenant_id, status)` for efficient active-server lookups

## Task-Specific Shared Contract

- Treat `docs/design-docs/phase-2/track-4-custom-tool-runtime.md` Data Model section as the canonical schema contract.
- Server `name` is used for tool namespacing (`server_name__tool_name`), so it must be stable and unique per tenant.
- `auth_token` is stored as plaintext in Track 4, consistent with how `provider_keys.api_key` works in Phase 1. Secrets Manager migration is deferred to Phase 3+.
- `server_id` is a UUID primary key (not a composite key like agents).
- `status` supports `active` and `disabled` values.
- `auth_type` supports `none` and `bearer_token` values.

## Affected Component

- **Service/Module:** Database Schema
- **File paths:**
  - `infrastructure/database/migrations/0008_tool_servers.sql` (new)
- **Change type:** new migration

## Dependencies

- **Must complete first:** None (entry point task)
- **Provides output to:** Task 2 (Tool Server API), Task 3 (Agent Config Extension), Task 4 (MCP Session Manager), Task 5 (Executor Integration), Task 6 (Console — Tool Servers), Task 7 (Console — Agent Config), Task 8 (Integration Tests)
- **Shared interfaces/contracts:** PostgreSQL schema contract for tool server registry

## Implementation Specification

### Step 1: Create the tool_servers table

```sql
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
```

- `server_id`: UUID primary key, auto-generated
- `tenant_id`: tenant scoping, defaults to `'default'`
- `name`: human-readable identifier, must match `[a-z0-9][a-z0-9-]*` (lowercase alphanumeric + hyphens, no leading hyphen)
- `url`: MCP server HTTP endpoint URL
- `auth_type`: `none` (no auth) or `bearer_token` (Authorization: Bearer header)
- `auth_token`: bearer token value, nullable (only set when auth_type = 'bearer_token')
- `status`: `active` or `disabled`
- `created_at`, `updated_at`: standard timestamps

### Step 2: Add unique constraint

```sql
-- Step 2: Unique constraint — server names must be unique within a tenant
ALTER TABLE tool_servers ADD CONSTRAINT uq_tool_servers_tenant_name UNIQUE (tenant_id, name);
```

### Step 3: Create indexes

```sql
-- Step 3: Index for efficient lookup of active servers for a tenant
CREATE INDEX idx_tool_servers_tenant_status ON tool_servers (tenant_id, status);
```

### Step 4: Add comment for documentation

```sql
-- Step 4: Table comment
COMMENT ON TABLE tool_servers IS 'Registry of external MCP tool servers. Operators register servers by URL; agents reference them by name.';
```

## Acceptance Criteria

- [ ] Migration `0008_tool_servers.sql` applies cleanly on a fresh database after migrations 0001-0007
- [ ] `tool_servers` table exists with all columns: `server_id` (UUID PK), `tenant_id`, `name`, `url`, `auth_type`, `auth_token`, `status`, `created_at`, `updated_at`
- [ ] `name` CHECK constraint enforces `^[a-z0-9][a-z0-9-]*$` pattern
- [ ] `auth_type` CHECK constraint allows only `none` and `bearer_token`
- [ ] `status` CHECK constraint allows only `active` and `disabled`
- [ ] Unique constraint `uq_tool_servers_tenant_name` on `(tenant_id, name)` prevents duplicate registrations
- [ ] Index `idx_tool_servers_tenant_status` on `(tenant_id, status)` exists
- [ ] `server_id` defaults to `gen_random_uuid()`
- [ ] `tenant_id` defaults to `'default'`
- [ ] `auth_type` defaults to `'none'`
- [ ] `status` defaults to `'active'`
- [ ] Existing test seeds still load successfully
- [ ] `make db-reset` completes without errors

## Testing Requirements

- **Integration tests:** Apply all migrations 0001-0008 in sequence on a fresh PostgreSQL container. Verify `tool_servers` table is writable. Verify unique constraint rejects duplicate `(tenant_id, name)`. Verify CHECK constraints reject invalid values.
- **Failure scenarios:** INSERT with `name = 'UPPERCASE'` must fail (regex constraint). INSERT with `auth_type = 'oauth2'` must fail. INSERT with `status = 'deleted'` must fail. INSERT duplicate `(tenant_id, name)` must fail with unique violation. INSERT with `name = '-leading-hyphen'` must fail.

## Constraints and Guardrails

- Do not modify existing migration files (0001-0007). All schema changes go in `0008_tool_servers.sql`.
- Do not add columns not specified in this task.
- Do not implement any application-level logic — this task is schema-only.
- Use `-- Step N:` comment headers in the migration file to match the convention in `0005`, `0006`, and `0007`.

## Assumptions

- The migration runs after `0007_scheduler_and_budgets.sql` (Track 3) has been applied.
- The naming convention `^\d{4}_.*\.sql$` is followed for automatic pickup by the schema-bootstrap ledger.
- The `name` regex constraint `^[a-z0-9][a-z0-9-]*$` ensures names are safe for use in tool namespacing (`server_name__tool_name`).

<!-- AGENT_TASK_END: task-1-database-migration.md -->
