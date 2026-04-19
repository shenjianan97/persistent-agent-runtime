-- 0015: Add context_exceeded_irrecoverable to dead_letter_reason CHECK constraint.
-- Track 7 — Context Window Management hard-floor safety net.
--
-- When Tier 1 + 1.5 + 3 compaction together cannot bring estimated input tokens
-- below the model's context window, the task is dead-lettered with this reason.
-- Expected to be rare in practice given the 25 KB per-tool-result cap.
--
-- task_events does NOT have a dead_letter_reason column — no second constraint
-- to update here. Dead-letter reason is stored in tasks.dead_letter_reason and
-- captured in task_events.details JSONB.

ALTER TABLE tasks DROP CONSTRAINT tasks_dead_letter_reason_check;
ALTER TABLE tasks ADD CONSTRAINT tasks_dead_letter_reason_check
    CHECK (dead_letter_reason IS NULL OR dead_letter_reason IN (
        'cancelled_by_user',
        'retries_exhausted',
        'task_timeout',
        'non_retryable_error',
        'max_steps_exceeded',
        'human_input_timeout',
        'rejected_by_user',
        'sandbox_lost',
        'sandbox_provision_failed',
        'context_exceeded_irrecoverable'
    ));
