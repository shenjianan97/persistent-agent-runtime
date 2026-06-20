-- 0026: Admit the Supervisor-topology terminal SUCCESS marker
-- ``subagent_completed`` into the task_events.event_type whitelist (Agent Modes,
-- Supervisor Topology — sub-agent completion visibility).
--
-- The fan-out emit path (executor/supervisor/graph.py _fanout_node, success
-- branch) writes one ``subagent_completed`` row per sub-agent that finishes
-- SUCCESSFULLY — the counterpart to ``subagent_failed`` (migration 0025). It is
-- the terminal half of the lifecycle bracket ``subagent_started`` →
-- ``subagent_completed`` | ``subagent_failed``. Without it, a sub-agent that
-- succeeds but produces ZERO findings emits nothing after ``subagent_started``,
-- so the Console badge is stranded on "running" forever.
--
--   * subagent_completed    — a sub-agent finished successfully.
--                             details: {iteration, subtask}  (NO finding_count or
--                             other result — findings ride their own
--                             subagent_finding rows; §A7 keeps result data off
--                             lifecycle markers)
--
-- Pattern A (plan §A4): iteration (int round, 1-based) and subtask (string
-- logical id "<iteration>.<index>") ride in the EXISTING details JSONB. NO new
-- columns, NO sub-agent task row, NO backfill — existing rows are unaffected.
--
-- Deploy-order (§A6): this migration MUST land in production *before* any worker
-- build that emits subagent_completed reaches prod — otherwise the
-- INSERT INTO task_events violates the CHECK constraint.
--
-- Lock-safety (matches migration 0025): the naïve DROP + re-ADD pattern
-- (migrations 0020/0024) takes ACCESS EXCLUSIVE on task_events AND re-validates
-- every existing row under that heavy lock — a write/read stall that scales with
-- table size. This migration instead uses the weaker-lock two-step path:
--   1. ADD CONSTRAINT ..._v2 ... NOT VALID  — admits new inserts immediately and
--      skips the full-table scan under the heavy lock.
--   2. VALIDATE CONSTRAINT ..._v2           — takes only SHARE UPDATE EXCLUSIVE
--      (does not block reads/writes) to re-check existing rows.
--   3. DROP the old constraint, then rename _v2 to the canonical name so the
--      end state matches the 0020/0024/0025 naming convention.
-- End state: the full 25-value 0025 allowlist + subagent_completed = 26 values,
-- under the canonical name task_events_event_type_check.

-- Step 1: add the full target allowlist as a NOT VALID constraint (no full scan,
-- no ACCESS EXCLUSIVE re-validate; new inserts are checked immediately).
ALTER TABLE task_events ADD CONSTRAINT task_events_event_type_check_v2 CHECK (event_type IN (
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
    'memory_written',
    'subagent_started',
    'subagent_finding',
    'subagent_failed',
    'supervisor_iteration',
    'subagent_completed'
)) NOT VALID;

-- Step 2: validate existing rows under the lighter SHARE UPDATE EXCLUSIVE lock
-- (does not block concurrent reads/writes).
ALTER TABLE task_events VALIDATE CONSTRAINT task_events_event_type_check_v2;

-- Step 3: drop the old constraint and rename _v2 to the canonical name so the
-- end state is a single CHECK named task_events_event_type_check (matching the
-- 0020/0024/0025 convention the next additive migration will DROP+re-ADD or _v2).
ALTER TABLE task_events DROP CONSTRAINT task_events_event_type_check;
ALTER TABLE task_events RENAME CONSTRAINT task_events_event_type_check_v2
    TO task_events_event_type_check;

COMMENT ON CONSTRAINT task_events_event_type_check
    ON task_events IS
    'Agent Modes (Supervisor Topology): admits subagent_completed — the terminal '
    'SUCCESS marker (counterpart to subagent_failed) — alongside the 0025 '
    'subagent_started / subagent_finding / subagent_failed / supervisor_iteration '
    'markers and the existing lifecycle / HITL / compaction / memory markers, so '
    'the Activity timeline surfaces sub-agent fan-out completion as append-only '
    'sub-steps (Pattern A — iteration/subtask ride in details JSONB, no new '
    'columns). subagent_heartbeat is deliberately NOT admitted (it is a Langfuse '
    'span event, §A11-E4).';
