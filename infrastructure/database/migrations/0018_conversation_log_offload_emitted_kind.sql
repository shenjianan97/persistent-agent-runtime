-- Phase 2 Track 7 Follow-up Task 5: add `offload_emitted` to the
-- `task_conversation_log.kind` CHECK constraint so the user-facing
-- conversation log can carry one entry per Tier-0 ingestion-offload pass.
--
-- The entry is emitted once per pass that offloaded ≥1 item (see
-- services/worker-service/core/conversation_log_repository.py and
-- services/worker-service/executor/graph.py). Payload shape:
--     content  = { "count": <int>, "total_bytes": <int>, "step_index": <int> }
--     metadata = {}                         -- v1 leaves metadata empty
--
-- Track 2 DROP+ADD pattern: we cannot ALTER an existing CHECK constraint
-- in place, so this migration drops the named constraint and re-adds it
-- with the expanded kind set. No data rewrite is required because no
-- existing row uses the new kind.

ALTER TABLE task_conversation_log
    DROP CONSTRAINT chk_task_conversation_log_kind;

ALTER TABLE task_conversation_log
    ADD CONSTRAINT chk_task_conversation_log_kind
    CHECK (kind IN (
        'user_turn',
        'agent_turn',
        'tool_call',
        'tool_result',
        'system_note',
        'compaction_boundary',
        'memory_flush',
        'hitl_pause',
        'hitl_resume',
        'offload_emitted'
    ));

COMMENT ON CONSTRAINT chk_task_conversation_log_kind
    ON task_conversation_log IS
    'Phase 2 Track 7 Follow-up Task 5: extended to include offload_emitted '
    '(one entry per ingestion-offload pass that moved ≥1 tool result / arg '
    'to S3). Expand via DROP+ADD when adding future kinds.';
