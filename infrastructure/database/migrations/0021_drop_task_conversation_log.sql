-- Phase 2 Track 7 Follow-up Task 8 Phase D — cleanup migration.
--
-- PR #93 shipped the unified Activity projection (task_events + checkpoints);
-- the legacy user-facing conversation log store is no longer read or written.
-- This migration drops the supporting table and its index.
--
-- Historical note: migrations 0017 (table creation) and 0018 (offload_emitted
-- kind addition) are now obsoleted but remain on disk intentionally — migration
-- history is immutable in this project so that `make e2e-test` can replay any
-- point in time. The DROP below is idempotent via IF EXISTS so re-running a
-- fresh environment after 0021 stays a no-op on the legacy objects.
--
-- The composite unique index uq_tasks_task_tenant on tasks(task_id, tenant_id)
-- added by 0017 is intentionally left in place; the column pair is used by
-- other tenant-isolation joins and may be useful elsewhere.
--
-- task_conversation_log was the only referrer of tasks via an ON DELETE CASCADE
-- FK — no other table references it, so the drop is safe with CASCADE
-- behaviour still honoured for any already-deleted parent tasks.

DROP INDEX IF EXISTS idx_task_conversation_log_task_seq;

DROP TABLE IF EXISTS task_conversation_log;
