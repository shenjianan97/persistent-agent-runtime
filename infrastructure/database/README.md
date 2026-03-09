# Phase 1 Database Schema

This directory contains the canonical PostgreSQL bootstrap artifacts for Phase 1 durable execution.

## Files

- `migrations/0001_phase1_durable_execution.sql`: initial schema for `tasks`, `checkpoints`, and `checkpoint_writes`
- `migrations/0002_worker_registry.sql`: `workers` table for worker self-registration and heartbeat tracking
- `tests/verification.sql`: integration-style verification of the shipped schema and canonical query patterns
- `verify_schema.sh`: launches or reuses a disposable PostgreSQL container and runs the verification suite

## Contract Boundaries

- The schema contract is defined by `docs/design/PHASE1_DURABLE_EXECUTION.md`, Section 6.1.
- `tasks.status` values are exactly `queued`, `running`, `completed`, `dead_letter`.
- `dead_letter_reason` values are exactly `cancelled_by_user`, `retries_exhausted`, `task_timeout`, `non_retryable_error`, `max_steps_exceeded`.
- `updated_at` uses `DEFAULT NOW()` for inserts only. Application update queries must set `updated_at = NOW()` explicitly. There are no triggers or database functions that maintain `updated_at`.

## LISTEN/NOTIFY

`LISTEN/NOTIFY` is application-level behavior, not schema-level behavior. This schema does not create any trigger or function for notifications.

Application code must call `pg_notify('new_task', worker_pool_id)` inline in the same transaction that transitions a task to `status = 'queued'` for:

- task submission
- retry requeue
- reaper reclaim
- dead-letter redrive

Those SQL patterns are part of the downstream contract for the API Service and Worker Service.

## Verification

Warning: `verify_schema.sh` is destructive. It resets the `public` schema with `DROP SCHEMA IF EXISTS public CASCADE; CREATE SCHEMA public;` before applying the migration and running verification. Do not point it at a database whose contents you want to keep.

Run:

```bash
./infrastructure/database/verify_schema.sh
```

To keep the disposable PostgreSQL container for inspection after the run:

```bash
KEEP_DB_CONTAINER=1 ./infrastructure/database/verify_schema.sh
```

Then inspect it with:

```bash
docker ps -a --filter name=persistent-agent-runtime-postgres
docker logs persistent-agent-runtime-postgres
```

The script starts a disposable PostgreSQL container if needed, applies all migrations in order from `migrations/`, and runs verification coverage for:

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
