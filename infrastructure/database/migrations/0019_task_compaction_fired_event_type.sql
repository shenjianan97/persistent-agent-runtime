-- Add 'task_compaction_fired' to the task_events event_type CHECK constraint.
-- Emitted from the worker whenever Tier 3 compaction fires; surfaces the
-- event in the Console Execution History tab alongside HITL markers.

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
    'task_follow_up',
    'task_compaction_fired'
));
