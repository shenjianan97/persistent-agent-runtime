-- 0024: Admit ``memory_written`` into the task_events.event_type whitelist.
--
-- Activity-timeline marker emitted by the worker when a memory entry is
-- UPSERTed into agent_memory_entries (issue #102 follow-up). One row per
-- memory-writing task; emitted inside the same transaction as the
-- agent_memory_entries UPSERT so the marker either lands atomically with
-- the memory row or rolls back together. No dedup logic needed in the
-- worker because the UPSERT already guarantees one row per task_id.
--
-- Pattern mirrors migration 0020 — DROP+ADD on a single CHECK constraint
-- so the whole bundle moves in one migration. Future markers added in the
-- same way.

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
    'task_compaction_fired',
    'memory_flush',
    'system_note',
    'offload_emitted',
    'memory_written'
));

COMMENT ON CONSTRAINT task_events_event_type_check
    ON task_events IS
    'Issue #102 follow-up: admits memory_written alongside existing '
    'lifecycle / HITL / compaction markers so the Activity timeline '
    'surfaces successful memory commits.';
