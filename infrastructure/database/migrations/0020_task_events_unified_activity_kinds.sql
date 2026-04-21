-- Phase 2 Track 7 Follow-up Task 8 (A) — unify Conversation + Timeline.
--
-- Admit the marker kinds currently unique to ``task_conversation_log`` into
-- ``task_events.event_type`` so the unified Activity projection can read
-- user-visible markers from a single store.
--
-- New kinds in this bundle:
--   * memory_flush    — emitted once per MemoryFlushFiredEvent from the
--                       compaction pre_model_hook.
--   * system_note     — platform/system-generated notes (reserved for future
--                       use; defined here so producers do not require a
--                       per-kind migration).
--   * offload_emitted — emitted once per Tier-0 ingestion-offload pass that
--                       moved ≥1 payload to S3 (matches the convlog kind
--                       introduced in migration 0018).
--
-- Kinds already emitted with equivalent semantics (no new event_type needed):
--   * compaction_boundary → ``task_compaction_fired`` (details gain
--                            ``summary_text`` in this project's worker diff).
--   * hitl_pause          → ``task_paused`` / ``task_approval_requested`` /
--                            ``task_input_requested`` (details enriched).
--   * hitl_resume         → ``task_approved`` / ``task_rejected`` /
--                            ``task_input_received`` (already emitted from
--                            the API-service resume path).
--
-- Pattern: Track 2 DROP+ADD. One migration for the whole bundle — future
-- per-kind cost only applies when new kinds are introduced beyond this set.

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
    'offload_emitted'
));

COMMENT ON CONSTRAINT task_events_event_type_check
    ON task_events IS
    'Phase 2 Track 7 Follow-up Task 8: admits memory_flush, system_note, '
    'and offload_emitted alongside existing lifecycle / HITL / compaction '
    'markers so the Activity projection reads a single store.';
