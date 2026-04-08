-- Phase 2 Track 4: Task Follow-Up
-- Adds 'task_follow_up' to the task_events event_type CHECK constraint.

-- Step 1: Drop the existing CHECK constraint and recreate with the new value
ALTER TABLE task_events DROP CONSTRAINT task_events_event_type_check;

ALTER TABLE task_events ADD CONSTRAINT task_events_event_type_check CHECK (event_type IN (
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
    'task_cancelled',
    'task_follow_up'
));
