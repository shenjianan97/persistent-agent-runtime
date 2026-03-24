-- Migration 0004: Add timeout_reference_at to support correct timeout calculation on redriven tasks.
--
-- Problem: The reaper used created_at for timeout checks. When a dead-lettered task is
-- redriven, created_at is unchanged, so the reaper immediately re-dead-letters it if
-- the original creation time + task_timeout_seconds has already elapsed.
--
-- Fix: timeout_reference_at defaults to created_at and is reset to NOW() on each redrive,
-- giving the task a fresh timeout window after being redriven.

ALTER TABLE tasks ADD COLUMN timeout_reference_at TIMESTAMPTZ;
UPDATE tasks SET timeout_reference_at = created_at;
ALTER TABLE tasks ALTER COLUMN timeout_reference_at SET NOT NULL;
ALTER TABLE tasks ALTER COLUMN timeout_reference_at SET DEFAULT NOW();
