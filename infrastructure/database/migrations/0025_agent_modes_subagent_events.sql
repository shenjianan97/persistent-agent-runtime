-- 0025: Admit the four Supervisor-topology fan-out markers into the
-- task_events.event_type whitelist (Agent Modes, Supervisor Topology — S9).
--
-- A Deep Research run is ONE task row. Sub-agent activity surfaces as
-- append-only sub-steps on the parent's task_events timeline, never as separate
-- task rows (Pattern A, plan §A0 invariant 1). This migration admits the four
-- event_type values the fan-out emit path writes:
--
--   * subagent_started      — a sub-agent subgraph is dispatched in a fan-out
--                             super-step. details: {iteration, subtask,
--                             prompt_preview, tool_allowlist, depth}
--   * subagent_finding      — a sub-agent emits a structured finding.
--                             details: {iteration, subtask, finding_id,
--                             source_url}  (claim/quote ride the Langfuse span,
--                             NOT the row — bounds row size, §A7)
--   * subagent_failed       — a sub-agent exhausts its ceiling/timeout or errors.
--                             details: {iteration, subtask, reason} where
--                             reason ∈ {ceiling, timeout, error}
--   * supervisor_iteration  — the Supervisor closes a round and decides
--                             continue/stop. details: {iteration,
--                             subtasks_emitted, decision, reason} where
--                             decision ∈ {continue, stop}
--
-- Pattern A (plan §A4): iteration (int round, 1-based — matches S6's reducer,
-- which mints iteration = prev_iteration + 1 with prev starting at 0) and
-- subtask (string logical id "<iteration>.<index>") ride in the EXISTING
-- details JSONB. NO new columns, NO parent_task_id / sub_agent_id, NO
-- sub-agent task row, NO backfill — existing rows are unaffected.
--
-- DELIBERATELY NOT admitted: subagent_heartbeat (or any heartbeat value). The
-- sub-agent heartbeat is a Langfuse span event, NOT a task_events row
-- (§A11-E4 / executor/subagents/fanout.py SUBAGENT_HEARTBEAT_EVENT). It must
-- never become an admitted event_type.
--
-- Lock-safety (E10, plan §A11-E10): the naïve DROP + re-ADD pattern (migrations
-- 0020/0024) takes ACCESS EXCLUSIVE on task_events AND re-validates every
-- existing row under that heavy lock — a write/read stall that scales with
-- table size. This migration instead uses the weaker-lock two-step path:
--   1. ADD CONSTRAINT ..._v2 ... NOT VALID  — admits new inserts immediately and
--      skips the full-table scan under the heavy lock.
--   2. VALIDATE CONSTRAINT ..._v2           — takes only SHARE UPDATE EXCLUSIVE
--      (does not block reads/writes) to re-check existing rows.
--   3. DROP the old constraint, then rename _v2 to the canonical name so the
--      end state matches the 0020/0024 naming convention.
-- End state: the full 21-value 0024 allowlist + the four additions = 25 values,
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
    'supervisor_iteration'
)) NOT VALID;

-- Step 2: validate existing rows under the lighter SHARE UPDATE EXCLUSIVE lock
-- (does not block concurrent reads/writes).
ALTER TABLE task_events VALIDATE CONSTRAINT task_events_event_type_check_v2;

-- Step 3: drop the old constraint and rename _v2 to the canonical name so the
-- end state is a single CHECK named task_events_event_type_check (matching the
-- 0020/0024 convention the next additive migration will DROP+re-ADD or _v2).
ALTER TABLE task_events DROP CONSTRAINT task_events_event_type_check;
ALTER TABLE task_events RENAME CONSTRAINT task_events_event_type_check_v2
    TO task_events_event_type_check;

COMMENT ON CONSTRAINT task_events_event_type_check
    ON task_events IS
    'Agent Modes (Supervisor Topology, S9): admits subagent_started / '
    'subagent_finding / subagent_failed / supervisor_iteration alongside the '
    'existing lifecycle / HITL / compaction / memory markers so the Activity '
    'timeline surfaces sub-agent fan-out as append-only sub-steps (Pattern A — '
    'iteration/subtask ride in details JSONB, no new columns). subagent_heartbeat '
    'is deliberately NOT admitted (it is a Langfuse span event, §A11-E4).';
