# Phase 1 Database Schema

This directory contains the canonical PostgreSQL bootstrap artifacts for Phase 1 durable execution.

## Files

- `migrations/0001_phase1_durable_execution.sql`: initial schema for `tasks`, `checkpoints`, and `checkpoint_writes`
- `migrations/0002_worker_registry.sql`: `workers` table for worker self-registration and heartbeat tracking
- `migrations/0003_dynamic_models.sql`: provider keys and model pricing tables
- `tests/verification.sql`: integration-style verification of the shipped schema and canonical query patterns
- `make db-reset-verify`: launches or reuses the local PostgreSQL container, reapplies the canonical versioned schema migrations, and runs the verification suite

## Contract Boundaries

- The schema contract is defined by `docs/design-docs/phase-1/design.md`, Section 6.1.
- `tasks.status` values are exactly `queued`, `running`, `completed`, `dead_letter`.
- `dead_letter_reason` values are exactly `cancelled_by_user`, `retries_exhausted`, `task_timeout`, `non_retryable_error`, `max_steps_exceeded`.
- `updated_at` uses `DEFAULT NOW()` for inserts only. Application update queries must set `updated_at = NOW()` explicitly. There are no triggers or database functions that maintain `updated_at`.
- Both `checkpoints` and `checkpoint_writes` reference `tasks(task_id)` with `ON DELETE CASCADE`. Deleting a task automatically removes all associated checkpoint data.

## Deployment pre-flight: pgvector (Phase 2 Track 5)

Migration `0011_agent_memory.sql` runs `CREATE EXTENSION IF NOT EXISTS vector`. Before rolling this migration to any non-dev Postgres, verify two things on the target:

1. **pgvector is installed.** Managed offerings differ:
   - AWS RDS PG ≥ 15 default param group: pgvector is pre-installed and requires `rds_superuser`.
   - Aurora Postgres: same.
   - Self-hosted: the `postgresql-16-pgvector` (or equivalent) package must be present.
   Confirm with `SELECT extname FROM pg_extension WHERE extname = 'vector';` or, if not yet created, `SELECT * FROM pg_available_extensions WHERE name = 'vector';`.
2. **The deploy role can create extensions.** `CREATE EXTENSION vector` requires the role that runs migrations to have the relevant privilege (`rds_superuser` on RDS, `CREATE` on the database for self-hosted).

Dev and CI pin `pgvector/pgvector:pg16` across `docker-compose.yml`, `Makefile`'s `E2E_PG_IMAGE`, and `.github/workflows/ci.yml` service containers — so the image is already correct for those paths. The pre-flight above is only for shared environments the repo doesn't pin.

If pgvector isn't available on the target, migration `0011` fails loud on `CREATE EXTENSION vector` — which is the intended behavior. Don't silently skip it; install pgvector or hold the rollout.

## LISTEN/NOTIFY

`LISTEN/NOTIFY` is application-level behavior, not schema-level behavior. This schema does not create any trigger or function for notifications.

Application code must call `pg_notify('new_task', worker_pool_id)` inline in the same transaction that transitions a task to `status = 'queued'` for:

- task submission
- retry requeue
- reaper reclaim
- dead-letter redrive

Those SQL patterns are part of the downstream contract for the API Service and Worker Service.

## Verification

Warning: `make db-reset-verify` is destructive. It resets the `public` schema with `DROP SCHEMA IF EXISTS public CASCADE; CREATE SCHEMA public;` before applying the migrations and running verification. Do not point it at a database whose contents you want to keep. Use `make db-migrate` for safe, additive migrations; it records applied files in `schema_migrations` and skips them on subsequent runs.

Run:

```bash
make db-reset-verify
```

To ensure the local PostgreSQL container is running for inspection after verification:

```bash
make db-up
```

Then inspect it with:

```bash
docker ps -a --filter name=persistent-agent-runtime-postgres
docker logs persistent-agent-runtime-postgres
```

The Make targets start the local PostgreSQL container if needed, apply the numbered migrations in order from `migrations/`, and run verification coverage for:

- schema creation and required indexes
- claim via `FOR UPDATE SKIP LOCKED`
- heartbeat lease extension
- lease-aware checkpoint writes
- checkpoint pending writes storage
- retry requeue with `retry_after`
- reaper reclaim
- timeout dead-lettering
- cancellation
- redrive
- constraint failures for invalid `status`, invalid `dead_letter_reason`, and malformed inserts
