-- Agent Capabilities Track 2: E2B Sandbox Support
-- Adds sandbox_id column to tasks and extends dead_letter_reason for sandbox failures.

-- Step 1: Add sandbox_id column to tasks table
ALTER TABLE tasks ADD COLUMN sandbox_id TEXT;

-- Step 2: Expand dead_letter_reason CHECK constraint to include sandbox reasons
ALTER TABLE tasks DROP CONSTRAINT tasks_dead_letter_reason_check;
ALTER TABLE tasks ADD CONSTRAINT tasks_dead_letter_reason_check
    CHECK (dead_letter_reason IN (
        'cancelled_by_user',
        'retries_exhausted',
        'task_timeout',
        'non_retryable_error',
        'max_steps_exceeded',
        'human_input_timeout',
        'rejected_by_user',
        'sandbox_lost',
        'sandbox_provision_failed'
    ));

-- Step 3: Table comment for sandbox_id
COMMENT ON COLUMN tasks.sandbox_id IS 'E2B sandbox ID for reconnection on crash recovery. Set when sandbox is provisioned, cleared on task completion.';
