-- 0006_runtime_state_model.sql
-- Phase 2 Track 2: Runtime State Model — new statuses, task_events table, HITL columns

-- Step 1: Expand tasks status CHECK constraint to include HITL and pause states
ALTER TABLE tasks DROP CONSTRAINT tasks_status_check;
ALTER TABLE tasks ADD CONSTRAINT tasks_status_check
    CHECK (status IN ('queued', 'running', 'completed', 'dead_letter',
                      'waiting_for_approval', 'waiting_for_input', 'paused'));

-- Step 2: Expand dead_letter_reason CHECK constraint for HITL reasons
ALTER TABLE tasks DROP CONSTRAINT tasks_dead_letter_reason_check;
ALTER TABLE tasks ADD CONSTRAINT tasks_dead_letter_reason_check
    CHECK (dead_letter_reason IN ('cancelled_by_user', 'retries_exhausted', 'task_timeout',
                                   'non_retryable_error', 'max_steps_exceeded',
                                   'human_input_timeout', 'rejected_by_user'));

-- Step 3: Add new columns for HITL workflow state
ALTER TABLE tasks ADD COLUMN pending_input_prompt TEXT;
ALTER TABLE tasks ADD COLUMN pending_approval_action JSONB;
ALTER TABLE tasks ADD COLUMN human_input_timeout_at TIMESTAMPTZ;
ALTER TABLE tasks ADD COLUMN human_response TEXT;

-- Step 4: Create task_events table (append-only audit trail)
CREATE TABLE task_events (
    event_id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id      TEXT NOT NULL,
    task_id        UUID NOT NULL REFERENCES tasks(task_id),
    agent_id       TEXT NOT NULL,
    event_type     TEXT NOT NULL CHECK (event_type IN (
        'task_submitted',
        'task_claimed',
        'task_retry_scheduled',
        'task_reclaimed_after_lease_expiry',
        'task_dead_lettered',
        'task_redriven',
        'task_completed',
        'task_paused',
        'task_resumed',
        'task_approval_requested',
        'task_approved',
        'task_rejected',
        'task_input_requested',
        'task_input_received',
        'task_cancelled'
    )),
    status_before  TEXT,
    status_after   TEXT,
    worker_id      TEXT,
    error_code     TEXT,
    error_message  TEXT,
    details        JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Step 5: Create indexes
-- Primary lookup: events for a specific task in chronological order
CREATE INDEX idx_task_events_task ON task_events (task_id, created_at);

-- Tenant-scoped listing (e.g., recent events across all tasks)
CREATE INDEX idx_task_events_tenant ON task_events (tenant_id, created_at DESC);

-- Reaper scan for human-input-timeout (partial index for efficiency)
CREATE INDEX idx_tasks_human_input_timeout ON tasks (human_input_timeout_at)
    WHERE human_input_timeout_at IS NOT NULL
      AND status IN ('waiting_for_approval', 'waiting_for_input');
