<!-- AGENT_TASK_START: task-1-db-migration.md -->

# Task 1 — Database Migration: Artifact Storage

## Agent Instructions

You are a software engineer implementing one module of a larger system. Your scope is strictly limited to this task. Do not modify components outside the "Affected Component" listed below.

**CRITICAL PRE-WORK:** Before beginning implementation, you MUST read:
1. `docs/design-docs/agent-capabilities/design.md` — canonical design contract (Section 3: Database Schema Changes, `task_artifacts` table)
2. `docs/exec-plans/active/agent-capabilities/track-1/plan.md` — Track 1 execution plan
3. `infrastructure/database/migrations/0008_tool_servers.sql` — latest existing migration (Track 4)
4. `infrastructure/database/migrations/0001_phase1_durable_execution.sql` — tasks table schema and conventions

**CRITICAL POST-WORK:** After completing this task:
1. Run the full test suite with `make test` and verify all existing tests still pass. Fix any regressions before proceeding.
2. Update the status in `docs/exec-plans/active/agent-capabilities/track-1/progress.md` to "Done".

## Context

Track 1 delivers end-to-end output artifact support. This is the foundational migration that creates the `task_artifacts` table for storing artifact metadata. All subsequent tasks (API repository, worker upload tool, console display) depend on this table schema.

This migration adds:
- A `task_artifacts` table for tracking files produced by agents (output) or attached by users (input)
- UNIQUE constraint on `(task_id, direction, filename)` to prevent duplicate artifacts
- Composite index on `(tenant_id, task_id)` for efficient tenant-scoped artifact listing

No sandbox-related changes in this task. No `sandbox_id` column, no `dead_letter_reason` extension — those belong to Track 2.

## Task-Specific Shared Contract

- Treat `docs/design-docs/agent-capabilities/design.md` Section 3 as the canonical schema contract.
- `artifact_id` is a UUID primary key with auto-generation via `gen_random_uuid()`.
- `task_id` is a foreign key referencing the `tasks` table.
- `tenant_id` is denormalized from the task row for efficient tenant-scoped queries.
- `direction` is either `'input'` or `'output'`. Track 1 only produces `output` artifacts; `input` is used by Track 2.
- `s3_key` stores the full S3 object key: `{tenant_id}/{task_id}/{direction}/{filename}`.
- `size_bytes` is BIGINT to support files up to 50 MB.

## Affected Component

- **Service/Module:** Database Schema
- **File paths:**
  - `infrastructure/database/migrations/0009_artifact_storage.sql` (new)
- **Change type:** new migration

## Dependencies

- **Must complete first:** None (entry point task, can run in parallel with Task 2)
- **Provides output to:** Task 3 (Worker S3 Client), Task 4 (API Artifact Repository + S3), Task 5 (API Artifact Endpoints), Task 6 (upload_artifact Tool), Task 7 (Console Artifacts Tab), Task 8 (Integration Tests)
- **Shared interfaces/contracts:** PostgreSQL schema contract for artifact storage

## Implementation Specification

### Step 1: Create the task_artifacts table

```sql
-- Step 1: Create task_artifacts table for artifact metadata
CREATE TABLE task_artifacts (
    artifact_id   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    task_id       UUID NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
    tenant_id     TEXT NOT NULL,
    filename      TEXT NOT NULL,
    direction     TEXT NOT NULL CHECK (direction IN ('input', 'output')),
    content_type  TEXT NOT NULL,
    size_bytes    BIGINT NOT NULL,
    s3_key        TEXT NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

- `artifact_id`: UUID primary key, auto-generated
- `task_id`: foreign key to `tasks` table, NOT NULL
- `tenant_id`: tenant scoping, denormalized from task for query efficiency
- `filename`: original filename of the artifact
- `direction`: `'input'` (user-uploaded) or `'output'` (agent-produced)
- `content_type`: MIME type (e.g., `text/plain`, `application/pdf`)
- `size_bytes`: file size in bytes
- `s3_key`: full S3 object key for retrieving the file
- `created_at`: timestamp of artifact creation

### Step 2: Add unique constraint

```sql
-- Step 2: Unique constraint — prevent duplicate artifacts per task/direction/filename
ALTER TABLE task_artifacts ADD CONSTRAINT uq_task_artifacts_task_direction_filename
    UNIQUE (task_id, direction, filename);
```

### Step 3: Create index

```sql
-- Step 3: Composite index for efficient tenant-scoped artifact listing
CREATE INDEX idx_task_artifacts_tenant_task ON task_artifacts (tenant_id, task_id);
```

### Step 4: Add comment for documentation

```sql
-- Step 4: Table comment
COMMENT ON TABLE task_artifacts IS 'Metadata for files produced by agents (output) or attached by users (input). Actual file content stored in S3.';
```

## Acceptance Criteria

- [ ] Migration `0009_artifact_storage.sql` applies cleanly on a fresh database after migrations 0001-0008
- [ ] `task_artifacts` table exists with all columns: `artifact_id` (UUID PK), `task_id` (UUID FK), `tenant_id` (TEXT), `filename` (TEXT), `direction` (TEXT), `content_type` (TEXT), `size_bytes` (BIGINT), `s3_key` (TEXT), `created_at` (TIMESTAMPTZ)
- [ ] `task_id` foreign key references `tasks(task_id) ON DELETE CASCADE`
- [ ] `direction` CHECK constraint allows only `'input'` and `'output'`
- [ ] Unique constraint `uq_task_artifacts_task_direction_filename` on `(task_id, direction, filename)` prevents duplicates
- [ ] Composite index `idx_task_artifacts_tenant_task` on `(tenant_id, task_id)` exists
- [ ] `artifact_id` defaults to `gen_random_uuid()`
- [ ] `created_at` defaults to `NOW()`
- [ ] Existing test seeds still load successfully
- [ ] `make db-reset-verify` completes without errors

## Testing Requirements

- **Integration tests:** Apply all migrations 0001-0009 in sequence on a fresh PostgreSQL container. Verify `task_artifacts` table is writable. Verify unique constraint rejects duplicate `(task_id, direction, filename)`. Verify CHECK constraint rejects invalid `direction` values. Verify FK constraint rejects non-existent `task_id`.
- **Failure scenarios:** INSERT with `direction = 'archive'` must fail (CHECK constraint). INSERT with duplicate `(task_id, direction, filename)` must fail (unique violation). INSERT with non-existent `task_id` must fail (FK violation).

## Constraints and Guardrails

- Do not modify existing migration files (0001-0008). All schema changes go in `0009_artifact_storage.sql`.
- Do not add columns not specified in this task (no `sandbox_id`, no `dead_letter_reason` changes).
- Do not implement any application-level logic — this task is schema-only.
- Use `-- Step N:` comment headers in the migration file to match the convention in `0005`, `0006`, `0007`, and `0008`.

## Assumptions

- The migration runs after `0008_tool_servers.sql` (Track 4) has been applied.
- The naming convention `^\d{4}_.*\.sql$` is followed for automatic pickup by the schema-bootstrap ledger.
- The `tasks` table already exists with a `task_id` UUID primary key (from migration `0001`).

<!-- AGENT_TASK_END: task-1-db-migration.md -->
