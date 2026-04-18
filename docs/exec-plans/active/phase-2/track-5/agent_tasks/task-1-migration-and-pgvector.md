<!-- AGENT_TASK_START: task-1-migration-and-pgvector.md -->

# Task 1 — Database Migration and pgvector Image Pin

## Agent Instructions

You are a software engineer implementing one module of a larger system. Your scope is strictly limited to this task. Do not modify components outside the "Affected Component" listed below.

**CRITICAL PRE-WORK:** Before beginning implementation, you MUST read:
1. `docs/design-docs/phase-2/track-5-memory.md` — sections "Data Model", "Embeddings", "Development Environment Assumption", and "Scale and Operational Plan".
2. `infrastructure/database/migrations/0010_sandbox_support.sql` — latest existing migration (template for file structure, constraint naming, comment headers).
3. `infrastructure/database/migrations/0005_agents_table.sql` — composite-key `(tenant_id, agent_id)` pattern that memory rows reference.
4. `infrastructure/database/migrations/0001_phase1_durable_execution.sql` — `tasks` table schema; memory rows soft-reference `task_id`.
5. `docker-compose.yml` — the `postgres` service (container name `persistent-agent-runtime-postgres`). **Do not look for a `par-dev-postgres` service — that string does not appear in the current docker-compose file.** Only the Makefile-driven test DB uses the `par-e2e-postgres` container name.
6. `Makefile` — the `test-db-up` target and any `E2E_PG_IMAGE` variable; this is a separate code path from `docker-compose.yml` and MUST be updated.
7. `.github/workflows/ci.yml` — any job that spins up Postgres as a service container instead of going through the Makefile.

**CRITICAL POST-WORK:** After completing this task:
1. Run `make db-reset` on a local dev Postgres and confirm all migrations apply cleanly, including `0011`.
2. Run `make test` and `make worker-test` / `make e2e-test`. Fix any regressions before proceeding.
3. Update the status in `docs/exec-plans/active/phase-2/track-5/progress.md` to "Done".

## Context

Track 5 is the first track in this codebase to require the `pgvector` Postgres extension. The stock `postgres:16` image used everywhere today does not ship pgvector — so without a coordinated image swap, `CREATE EXTENSION vector` in the new migration will fail on every environment at once.

This task delivers:

- A single numbered migration file creating both new tables, the `vector` extension, the generated `content_tsv` column, and all indexes (HNSW + GIN + btree).
- An image pin swap from `postgres:16` → `pgvector/pgvector:pg16` across local dev (`docker-compose.yml`), the Makefile-driven isolated test DB (`test-db-up` / `E2E_PG_IMAGE`), and any CI service container that bypasses the Makefile.
- A production / staging pre-flight note in this task's notes section so the deploy role's `CREATE EXTENSION` privilege is confirmed before the track ships. (This is a deploy blocker — if the managed Postgres instance does not allow the extension, the track cannot ship.)

## Task-Specific Shared Contract

- Treat `docs/design-docs/phase-2/track-5-memory.md` — "Data Model" and "Development Environment Assumption" sections — as the canonical schema contract.
- Embedding dimension is fixed at **1536** in v1. Do not parameterise it.
- The generated column MUST use the two-argument form `to_tsvector('english'::regconfig, …)` — the single-argument form is only `STABLE` on modern Postgres and cannot back a `STORED GENERATED` column.
- `content_vec` is nullable; deferred-embedding writes land with `NULL` and remain so until explicitly reindexed. Task 5 implements the deferred path.
- `agent_memory_entries.task_id` is a **soft reference** — no database-level FK to `tasks(task_id)`. Memory rows must survive a future task prune.
- `task_attached_memories.task_id` **does** have `ON DELETE CASCADE` to `tasks(task_id)`; `task_attached_memories.memory_id` is a **soft reference** — no FK to `agent_memory_entries(memory_id)`. Deleting a memory entry leaves the attachment audit row in place.
- The `(tenant_id, agent_id)` FK from `agent_memory_entries` to `agents(tenant_id, agent_id)` IS enforced at the database level.
- HNSW index uses `vector_cosine_ops` and pgvector's default parameters (`m=16`, `ef_construction=64`).
- Memory entries are capped at **10,000 per agent** by default. Do NOT implement the cap in SQL — it belongs to the worker commit logic (Task 6). Ship only the schema now.

## Affected Component

- **Service/Module:** Database schema + local/CI container images
- **File paths:**
  - `infrastructure/database/migrations/0011_agent_memory.sql` (new)
  - `docker-compose.yml` (modify — `postgres` service image)
  - `Makefile` (modify — `test-db-up` target / `E2E_PG_IMAGE` variable)
  - `.github/workflows/ci.yml` (modify — any direct Postgres service container image pin)
- **Change type:** new migration + targeted modifications to dev/CI image pins

## Dependencies

- **Must complete first:** None (entry point task)
- **Provides output to:** All other tasks (2–11)
- **Shared interfaces/contracts:** The schema of `agent_memory_entries` and `task_attached_memories`, and the presence of the `vector` extension

## Implementation Specification

### Schema contract

Deliver the two tables exactly as specified in the design doc's "Data Model" section:

- **`agent_memory_entries`** — columns: `memory_id` (UUID PK, default `gen_random_uuid()`), `tenant_id` (TEXT NOT NULL), `agent_id` (TEXT NOT NULL), `task_id` (UUID NOT NULL, UNIQUE, soft ref), `title` (TEXT NOT NULL, max 200 chars — enforce with `CHECK(length(title) <= 200)`), `summary` (TEXT NOT NULL), `observations` (TEXT[] NOT NULL DEFAULT `'{}'`), `outcome` (TEXT NOT NULL CHECK in `('succeeded','failed')`), `tags` (TEXT[] NOT NULL DEFAULT `'{}'`), `content_tsv` (generated STORED), `content_vec` (vector(1536), NULLABLE), `summarizer_model_id` (TEXT, nullable — NOT an FK to `models` because sentinel values `'template:fallback'` and `'template:dead_letter'` are stored here), `version` (INT NOT NULL DEFAULT 1), `created_at` (TIMESTAMPTZ NOT NULL DEFAULT `now()`), `updated_at` (TIMESTAMPTZ NOT NULL DEFAULT `now()`).
  - FK `(tenant_id, agent_id) REFERENCES agents(tenant_id, agent_id)`.
  - Indexes: btree `(tenant_id, agent_id, created_at DESC)`; GIN on `content_tsv`; HNSW on `content_vec` with `vector_cosine_ops`.
  - `UNIQUE (task_id)`.

- **`task_attached_memories`** — columns: `task_id` (UUID NOT NULL, FK to `tasks(task_id)` with `ON DELETE CASCADE`), `memory_id` (UUID NOT NULL — soft reference, NO FK), `position` (INT NOT NULL), `created_at` (TIMESTAMPTZ NOT NULL DEFAULT `now()`).
  - PK `(task_id, memory_id)`.
  - Additional index on `(memory_id)` for reverse lookup.

- **`tasks.skip_memory_write`** — add a new column `skip_memory_write BOOLEAN NOT NULL DEFAULT FALSE` to the existing `tasks` table. Task 4 and Task 6 both read this column (Task 4 persists the submission-time override; Task 6 gates the worker write on it). Column choice is a typed column rather than a JSONB key so the worker hot path avoids JSONB parse cost and the column is queryable for operational reporting. Add a `-- Step N: Extend tasks table with skip_memory_write override` header before the `ALTER TABLE`.

### Generated column requirement

```
content_tsv tsvector GENERATED ALWAYS AS (
  to_tsvector(
    'english'::regconfig,
    coalesce(title, '') || ' ' ||
    coalesce(summary, '') || ' ' ||
    array_to_string(observations, ' ') || ' ' ||
    array_to_string(tags, ' ')
  )
) STORED
```

Verify locally that the column is populated on an INSERT with realistic data (title + summary + observations array + tags array all set) and that a GIN query like `WHERE content_tsv @@ websearch_to_tsquery('english', 'foo')` uses the GIN index (check `EXPLAIN`).

### Migration file structure

- File name: `0011_agent_memory.sql` — matches the existing `[0-9][0-9][0-9][0-9]_*.sql` glob that the schema-bootstrap ledger auto-picks up.
- Open with `CREATE EXTENSION IF NOT EXISTS vector;` — idempotent, safe to re-run.
- Use `-- Step N:` comment headers per the convention in `0007` and `0010`.
- End with `COMMENT ON TABLE` lines describing each new table's purpose.
- Do NOT edit existing migration files.

### Image pin changes

- `docker-compose.yml`: swap `postgres:16` → `pgvector/pgvector:pg16` on the `postgres` service (the only Postgres service defined there; container name `persistent-agent-runtime-postgres`). Do not change port mappings, volume names, or any other config.
- `Makefile`: locate `test-db-up` / the `E2E_PG_IMAGE` variable (and any other Postgres docker-run inside the Makefile). Swap the image identically. Confirm `make e2e-test` brings up the container with the pgvector image.
- `.github/workflows/ci.yml`: if any workflow job defines Postgres as a service container directly (not via the Makefile), update that image pin. If every Postgres path in CI goes through the Makefile, leave CI untouched and document that in the PR description.

### Deploy-time pre-flight

Before this migration ships to staging/production, confirm the managed Postgres target supports `CREATE EXTENSION vector`:

- RDS ≥ PG 15 with the default parameter group: supported.
- Aurora Postgres: supported with `rds_superuser`.
- If the target does not support pgvector, the track cannot ship — escalate before merge.

Add a one-line note to the PR description that this check has been performed (or link the infrastructure ticket that confirms it).

## Acceptance Criteria

- [ ] Migration `0011_agent_memory.sql` applies cleanly on a fresh database after migrations 0001–0010, with no warnings.
- [ ] `vector` extension is installed (verify via `SELECT * FROM pg_extension WHERE extname='vector';`).
- [ ] `agent_memory_entries` and `task_attached_memories` tables exist with the columns, constraints, indexes, and FKs listed in "Schema contract" above.
- [ ] `content_tsv` is a generated STORED column using the two-argument `to_tsvector('english'::regconfig, …)` form.
- [ ] `content_vec` is `vector(1536)` and nullable.
- [ ] HNSW index on `content_vec` uses `vector_cosine_ops`.
- [ ] GIN index on `content_tsv` exists.
- [ ] `UNIQUE (task_id)` on `agent_memory_entries` exists.
- [ ] `(tenant_id, agent_id, created_at DESC)` btree index on `agent_memory_entries` exists.
- [ ] `(memory_id)` btree index on `task_attached_memories` exists.
- [ ] `tasks.skip_memory_write` column exists as `BOOLEAN NOT NULL DEFAULT FALSE`; existing rows default to `FALSE` on migration.
- [ ] Composite FK `(tenant_id, agent_id) → agents(tenant_id, agent_id)` is enforced — inserting a row with an unknown pair fails.
- [ ] `ON DELETE CASCADE` from `tasks(task_id)` to `task_attached_memories.task_id` is verified — deleting a task removes matching attachment rows, and a rollback-safe equivalent test proves this on the dedicated test DB.
- [ ] `docker-compose.yml`, `Makefile` (`test-db-up` and/or `E2E_PG_IMAGE`), and any direct CI service container pin are updated to `pgvector/pgvector:pg16`.
- [ ] `make db-reset` applies all migrations cleanly.
- [ ] `make test` passes with no regressions.
- [ ] `make worker-test` and `make e2e-test` pass — the test DB container now carries pgvector.
- [ ] Existing tests that issued `SELECT * FROM pg_extension` or similar are unaffected (or updated to expect the new extension).

## Testing Requirements

- **Migration test:** Apply all migrations 0001–0011 on a fresh Postgres container (pgvector image). INSERT + SELECT on both new tables succeeds. The generated `content_tsv` column is populated automatically. A GIN-backed `@@ websearch_to_tsquery` query returns the row. An HNSW-backed `<=> :vec` query returns the row when `content_vec` is set.
- **Constraint tests:** INSERT into `agent_memory_entries` with (a) unknown `(tenant_id, agent_id)` fails FK; (b) duplicate `task_id` fails unique; (c) `outcome='invalid'` fails check; (d) `length(title) > 200` fails. INSERT into `task_attached_memories` with unknown `task_id` fails FK.
- **Cascade test:** DELETE a task with a matching `task_attached_memories` row. Confirm the attachment row is gone.
- **Soft-reference test:** DELETE an `agent_memory_entries` row that has a matching `task_attached_memories.memory_id`. Confirm the attachment row remains and its `memory_id` still references the deleted memory id.
- **Generated column test:** UPDATE `observations` array — `content_tsv` should reflect the change.
- **CI smoke:** Run `make test-all` or its per-service equivalents locally. Everything passes, no test is looking for the absence of `vector`.

## Constraints and Guardrails

- Do not modify migrations 0001–0010.
- Do not add application-level logic anywhere in this task — schema + image pin only.
- Do not parameterise the embedding dimension (keep it hard-coded at 1536).
- Do not create additional partial indexes or partitioning in v1 — these are explicitly deferred in the design doc's "Scale and Operational Plan" section.
- Do not add the FIFO trim logic to the migration — it belongs to the worker commit path (Task 6).
- Do not introduce a new `models`-table row type or FK for `summarizer_model_id` — the column is intentionally free-form because it also stores template sentinels.

## Assumptions

- The migration runs after `0010_sandbox_support.sql`.
- The naming convention `^\d{4}_.*\.sql$` is followed for automatic pickup by the schema-bootstrap ledger.
- `pgvector/pgvector:pg16` is the canonical image pin for PG 16 with pgvector preinstalled, per the design doc.
- `CREATE EXTENSION IF NOT EXISTS vector` is idempotent — re-running the migration on a DB that already has the extension is safe.
- The test DB on port 55433 uses a fresh volume per run, so no data migration concern.

<!-- AGENT_TASK_END: task-1-migration-and-pgvector.md -->
