<!-- AGENT_TASK_START: task-1-database-schema.md -->

# Task 1: Database Schema — Langfuse Endpoints

## Agent Instructions
You are a software engineer implementing one module of a larger system.
Your scope is strictly limited to this task. Do not modify components outside
the "Affected Component" listed below.

**CRITICAL PRE-WORK:** Before beginning implementation, you MUST read the following context files:
1. `docs/design/langfuse-customer-integration/design.md`
2. `docs/design/PHASE1_DURABLE_EXECUTION.md`
3. `infrastructure/database/migrations/0001_phase1_durable_execution.sql` (existing schema patterns)

## Context
The Langfuse integration is being refactored from platform-owned infrastructure to a customer-owned integration. Customers will register their Langfuse endpoints, and tasks will optionally reference a registered endpoint. This task modifies the existing schema files directly to add the data foundation for that model.

The system is still in development — there is no production data to migrate. Backward compatibility is not a concern. Modify the existing schema files directly rather than creating a new migration.

## Task-Specific Shared Contract
- The `langfuse_endpoints` table stores customer-registered Langfuse instances with their credentials.
- The `tasks` table gains a nullable FK `langfuse_endpoint_id` so each task can optionally reference a Langfuse endpoint.
- `ON DELETE SET NULL` ensures deleting an endpoint doesn't orphan tasks — they simply lose their Langfuse reference.
- A local dev seed row is included for the default tenant to support local development with a test Langfuse instance.
- `tenant_id` follows the same pattern as existing tables (`TEXT NOT NULL`, default tenant is `'default'`).

## Affected Component
- **Service/Module:** Database Schema
- **File paths:** `infrastructure/database/migrations/0001_phase1_durable_execution.sql`, `infrastructure/database/migrations/test_seed.sql`
- **Change type:** modification of existing files

## Dependencies
- **Must complete first:** None
- **Provides output to:** Tasks 2, 3, 4, 5
- **Shared interfaces/contracts:** The `langfuse_endpoints` table schema and `tasks.langfuse_endpoint_id` column.

## Implementation Specification

### Step 1: Add `langfuse_endpoints` table to `0001_phase1_durable_execution.sql`

Add the `CREATE TABLE langfuse_endpoints` statement **before** the `CREATE TABLE tasks` statement (since `tasks` will reference it). Place it after the existing preamble comments and before the tasks table:

```sql
CREATE TABLE langfuse_endpoints (
    endpoint_id   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id     TEXT NOT NULL,
    name          TEXT NOT NULL,
    host          TEXT NOT NULL,
    public_key    TEXT NOT NULL,
    secret_key    TEXT NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (tenant_id, name)
);

CREATE INDEX idx_langfuse_endpoints_tenant ON langfuse_endpoints (tenant_id);
```

### Step 2: Add `langfuse_endpoint_id` column to the `tasks` table definition

In the existing `CREATE TABLE tasks` block in `0001_phase1_durable_execution.sql`, add:

```sql
    langfuse_endpoint_id  UUID REFERENCES langfuse_endpoints(endpoint_id) ON DELETE SET NULL,
```

Place it after `worker_pool_id` and before `created_at` (or wherever logically fits with the other optional/config fields).

### Step 3: Update `test_seed.sql`

Add the Langfuse endpoint seed and populate model cost rates (currently zero, which would make cost tracking useless):

```sql
-- Langfuse endpoint for local dev
INSERT INTO langfuse_endpoints (tenant_id, name, host, public_key, secret_key)
VALUES ('default', 'Local Dev', 'http://127.0.0.1:3300', 'pk-lf-local', 'sk-lf-local')
ON CONFLICT (tenant_id, name) DO NOTHING;
```

Also update the existing model seed to include realistic cost rates:

```sql
INSERT INTO models (provider_id, model_id, display_name, is_active,
                    input_microdollars_per_million, output_microdollars_per_million)
VALUES ('anthropic', 'claude-sonnet-4-6', 'Claude Sonnet 4.6', true, 3000000, 15000000)
ON CONFLICT (provider_id, model_id) DO UPDATE SET
    input_microdollars_per_million = EXCLUDED.input_microdollars_per_million,
    output_microdollars_per_million = EXCLUDED.output_microdollars_per_million;
```

Rate meaning: `input_microdollars_per_million = 3000000` means $3.00 per million input tokens (3,000,000 microdollars). This matches Anthropic's published pricing for Sonnet-class models.

### Step 4: Drop and recreate the local database

Since we're modifying the base schema, the local database must be recreated:
```bash
make db-down && make db-up
```

## Acceptance Criteria
- [ ] `0001_phase1_durable_execution.sql` contains the `langfuse_endpoints` table definition before the `tasks` table.
- [ ] `tasks` table definition includes `langfuse_endpoint_id UUID REFERENCES langfuse_endpoints(endpoint_id) ON DELETE SET NULL`.
- [ ] `\d langfuse_endpoints` shows all columns with correct types and constraints.
- [ ] `\d tasks` shows `langfuse_endpoint_id` as a nullable UUID FK.
- [ ] UNIQUE constraint prevents duplicate `(tenant_id, name)` pairs.
- [ ] Local dev seed row exists for tenant `'default'` after running `test_seed.sql`.
- [ ] `make db-down && make db-up` applies cleanly with no errors.

## Testing Requirements
- **Unit tests:** Not applicable to raw DDL.
- **Integration tests:** Recreate database, verify table structure, insert/update/delete operations, FK constraint behavior (ON DELETE SET NULL), and UNIQUE constraint enforcement.
- **Failure scenarios:** Verify UNIQUE constraint rejects duplicate `(tenant_id, name)`. Verify FK constraint rejects non-existent `endpoint_id` on task insert.

## Constraints and Guardrails
- Modify existing files directly — do NOT create a new `0005_*.sql` migration file.
- The `langfuse_endpoints` table must be defined before `tasks` in `0001` since `tasks` references it.
- `updated_at` uses `DEFAULT NOW()` for INSERT only. Application UPDATE queries must explicitly set `updated_at = NOW()`.
