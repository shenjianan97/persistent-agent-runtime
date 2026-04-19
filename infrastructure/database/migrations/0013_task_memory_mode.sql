-- Phase 2 Track 5 — Task 12: Replace per-task `skip_memory_write` BOOLEAN with
-- a three-value `memory_mode` TEXT enum on the tasks table.
--
-- Mode semantics (see
-- ``docs/exec-plans/active/phase-2/track-5/agent_tasks/task-12-task-memory-mode.md``
-- and ``docs/design-docs/phase-2/track-5-memory.md``):
--   * ``always``        — default: every successful task writes a memory
--                         (equivalent to today's ``skip_memory_write=false``).
--   * ``agent_decides`` — memory is written only if the agent calls the new
--                         ``save_memory(reason)`` tool during the run.
--   * ``skip``          — no memory is written for this task
--                         (equivalent to today's ``skip_memory_write=true``).
--
-- Backfill is value-preserving: rows submitted with ``skip_memory_write=true``
-- keep their skip intent as ``memory_mode='skip'`` through the migration.
-- Without this step, in-flight tasks (queued/running/paused) that asked to
-- skip memory would silently flip to ``always`` once the new column's DEFAULT
-- applies and then write a memory on completion — a privacy regression.
-- Terminal rows (``completed`` / ``dead_letter``) get the same mapping for
-- audit consistency.
--
-- The user has explicitly waived backward compatibility for the column name;
-- no dual-read path is preserved.

BEGIN;

ALTER TABLE tasks
    ADD COLUMN memory_mode TEXT NOT NULL
        DEFAULT 'always'
        CHECK (memory_mode IN ('always', 'agent_decides', 'skip'));

UPDATE tasks
SET memory_mode = CASE WHEN skip_memory_write THEN 'skip' ELSE 'always' END;

ALTER TABLE tasks DROP COLUMN skip_memory_write;

COMMENT ON COLUMN tasks.memory_mode IS
    'Phase 2 Track 5 Task 12: Per-task memory mode. One of always | agent_decides | skip. '
    'Replaces the previous skip_memory_write BOOLEAN. See '
    'docs/design-docs/phase-2/track-5-memory.md and '
    'docs/exec-plans/active/phase-2/track-5/agent_tasks/task-12-task-memory-mode.md.';

COMMIT;
