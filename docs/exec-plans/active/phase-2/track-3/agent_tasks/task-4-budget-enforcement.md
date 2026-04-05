<!-- AGENT_TASK_START: task-4-budget-enforcement.md -->

# Task 4 — Budget Enforcement at Checkpoint Boundaries

## Agent Instructions

You are a software engineer implementing one module of a larger system. Your scope is strictly limited to this task. Do not modify components outside the "Affected Component" listed below.

**CRITICAL PRE-WORK:** Before beginning implementation, you MUST read:
1. `docs/design-docs/phase-2/track-3-scheduler-and-budgets.md` — canonical design contract (Budget Model section, Pause and Resume Behavior section)
2. `services/worker-service/executor/graph.py` — current execution flow, `_handle_interrupt_internal()` pattern for pause transitions (lines 628-689), `_handle_dead_letter()` for terminal transitions, and the inline completion logic in `run_astream()` (lines 381-461). Note: there is no named `_handle_completion()` method — completion logic is inline.
3. `infrastructure/database/migrations/0007_scheduler_and_budgets.sql` — Task 1 output: task pause columns (`pause_reason`, `pause_details`, `resume_eligible_at`)

**CRITICAL POST-WORK:** After completing this task:
1. Run the full test suite with `make test` and verify all existing tests still pass. Fix any regressions before proceeding.
2. Update the status in `docs/exec-plans/active/phase-2/track-3/progress.md` to "Done".

## Context

Track 3 enforces budget limits at checkpoint-cost boundaries — the point after each LangGraph super-step where the checkpointer has durably written the new checkpoint and cost has been recorded (Task 2).

Budget enforcement never interrupts an in-flight model or tool call mid-step. Instead:
1. The current step finishes
2. Checkpoint and cost are written durably (Task 2)
3. Budget limits are checked
4. If exceeded, the task transitions from `running` to `paused`

Two budget limits are enforced:
- **Per-task budget** (`budget_max_per_task`): cumulative task cost exceeds agent's per-task limit → pause with `budget_per_task` reason → requires operator to increase budget and manually resume
- **Hourly budget** (`budget_max_per_hour`): rolling 60-minute agent-wide spend exceeds hourly limit → pause with `budget_per_hour` reason → auto-recovers when spend ages out of the window

## Task-Specific Shared Contract

- Budget enforcement happens after `_record_step_cost()` (Task 2) returns the cumulative task cost.
- If both hourly and per-task limits are exceeded, `budget_per_task` wins as the `pause_reason` (stricter recovery path).
- Budget pause uses the same `paused` status from Track 2, with `pause_reason`, `pause_details`, and `resume_eligible_at` fields.
- `paused` tasks do NOT count against `max_concurrent_tasks` — the `running_task_count` must be decremented on pause.
- Budget pause releases the lease (same pattern as HITL pause).
- `task_paused` event is emitted with budget details in the `details` JSONB field.
- Budget setting changes on an agent apply to the next checkpoint boundary of already-running tasks.

## Affected Component

- **Service/Module:** Worker Service — Executor
- **File paths:**
  - `services/worker-service/executor/graph.py` (modify — add budget check after cost recording, add budget-pause transition)
- **Change type:** modification

## Dependencies

- **Must complete first:** Task 1 (Database Migration — pause columns exist), Task 2 (Incremental Cost Tracking — `_record_step_cost()` provides cumulative cost and ledger entries)
- **Provides output to:** Task 5 (Reaper — auto-recovery for hourly pauses), Task 6 (API — resume endpoint), Task 8 (Integration Tests)
- **Shared interfaces/contracts:** Task pause columns (`pause_reason`, `pause_details`, `resume_eligible_at`), `agent_runtime_state.running_task_count` decrement on pause

## Implementation Specification

### Step 1: Add budget check method

Add `_check_budget_and_pause()` to `GraphExecutor`:

```python
async def _check_budget_and_pause(
    self,
    conn,
    task_data: dict,
    cumulative_task_cost: int,
    worker_id: str,
) -> bool:
    """Check budget limits after a checkpoint-cost write. Returns True if task was paused."""
    task_id = str(task_data["task_id"])
    tenant_id = task_data["tenant_id"]
    agent_id = task_data["agent_id"]

    # Re-read agent budget settings (may have changed since task started)
    agent = await conn.fetchrow(
        '''SELECT budget_max_per_task, budget_max_per_hour
           FROM agents WHERE tenant_id = $1 AND agent_id = $2''',
        tenant_id, agent_id
    )
    if not agent:
        return False

    budget_max_per_task = agent['budget_max_per_task']
    budget_max_per_hour = agent['budget_max_per_hour']

    # Check per-task budget (takes precedence if both exceeded)
    per_task_exceeded = cumulative_task_cost > budget_max_per_task

    # Check hourly budget (rolling 60-minute window from canonical ledger)
    hour_cost = await conn.fetchval(
        '''SELECT COALESCE(SUM(cost_microdollars), 0)
           FROM agent_cost_ledger
           WHERE tenant_id = $1 AND agent_id = $2
             AND created_at > NOW() - INTERVAL '60 minutes' ''',
        tenant_id, agent_id
    )
    hourly_exceeded = hour_cost > budget_max_per_hour

    if not per_task_exceeded and not hourly_exceeded:
        return False

    # Determine pause reason (per-task takes precedence)
    if per_task_exceeded:
        pause_reason = 'budget_per_task'
        pause_details = {
            'budget_max_per_task': budget_max_per_task,
            'observed_task_cost_microdollars': cumulative_task_cost,
            'recovery_mode': 'manual_resume_after_budget_increase'
        }
        resume_eligible_at = None
    else:
        pause_reason = 'budget_per_hour'
        pause_details = {
            'budget_max_per_hour': budget_max_per_hour,
            'observed_hour_cost_microdollars': hour_cost,
            'recovery_mode': 'automatic_after_window_clears'
        }
        # Estimate when enough spend ages out: find the oldest ledger entry
        # in the window and add 60 minutes
        oldest_entry_time = await conn.fetchval(
            '''SELECT MIN(created_at) FROM agent_cost_ledger
               WHERE tenant_id = $1 AND agent_id = $2
                 AND created_at > NOW() - INTERVAL '60 minutes' ''',
            tenant_id, agent_id
        )
        if oldest_entry_time:
            resume_eligible_at = oldest_entry_time + timedelta(minutes=60)
        else:
            resume_eligible_at = None

    await self._execute_budget_pause(
        conn, task_data, worker_id, pause_reason, pause_details, resume_eligible_at
    )
    return True
```

### Step 2: Implement budget-pause transition

Add `_execute_budget_pause()` to `GraphExecutor`:

```python
async def _execute_budget_pause(
    self,
    conn,
    task_data: dict,
    worker_id: str,
    pause_reason: str,
    pause_details: dict,
    resume_eligible_at: datetime | None,
):
    """Transition a running task to paused for budget exhaustion."""
    task_id = str(task_data["task_id"])
    tenant_id = task_data["tenant_id"]
    agent_id = task_data["agent_id"]

    # Atomically: update task, decrement running_task_count, record event
    # All three operations must be in the same transaction:
    async with conn.transaction():
      # 1. Transition task to paused (lease-validated)
      result = await conn.fetchrow(
        '''UPDATE tasks
           SET status = 'paused',
               pause_reason = $1,
               pause_details = $2::jsonb,
               resume_eligible_at = $3,
               lease_owner = NULL,
               lease_expiry = NULL,
               human_response = NULL,
               version = version + 1,
               updated_at = NOW()
           WHERE task_id = $4::uuid
             AND lease_owner = $5
           RETURNING task_id''',
        pause_reason,
        json.dumps(pause_details),
        resume_eligible_at,
        task_id,
        worker_id,
    )

      if not result:
          logger.warning("Budget pause failed for task %s: lease no longer owned", task_id)
          return

      # 2. Decrement running_task_count (use upsert for robustness — matches Task 5 pattern)
      await conn.execute(
          '''INSERT INTO agent_runtime_state (tenant_id, agent_id, running_task_count, hour_window_cost_microdollars, scheduler_cursor, updated_at)
             VALUES ($1, $2, 0, 0, '1970-01-01T00:00:00Z', NOW())
             ON CONFLICT (tenant_id, agent_id) DO UPDATE
             SET running_task_count = GREATEST(agent_runtime_state.running_task_count - 1, 0),
                 updated_at = NOW()''',
          tenant_id, agent_id
      )

      # 3. Record task_paused event
      # NOTE: _insert_task_event is a MODULE-LEVEL function, not a method — call without self.
      event_details = {
          'pause_reason': pause_reason,
          **pause_details
      }
      # Include resume_eligible_at in hourly pause event details (design doc stable contract)
      if resume_eligible_at:
          event_details['resume_eligible_at'] = resume_eligible_at.isoformat()
      await _insert_task_event(
          conn, task_id, tenant_id, agent_id,
          event_type='task_paused',
          status_before='running',
          status_after='paused',
          worker_id=worker_id,
          details=event_details
      )

    logger.info(
        "Task %s paused: %s (cost: %s)",
        task_id, pause_reason, pause_details
    )
```

Key implementation details:
- Lease-validated UPDATE: only pauses if `lease_owner` matches (prevents race with lease expiry requeue)
- `running_task_count` decrement uses `INSERT ... ON CONFLICT DO UPDATE` with `GREATEST(..., 0)` floor (consistent with Task 5 pattern)
- Event recording uses the existing `_insert_task_event` module-level function (NOT `self._insert_task_event` — it is NOT a method on GraphExecutor, see `graph.py` lines 792-818)
- Hourly pause event details include `resume_eligible_at` per the design doc's stable event contract (lines 488-494)
- All operations are wrapped in `async with conn.transaction():`
- **Claim-time hourly budget blocking is Task 3's responsibility** — this task only handles checkpoint-boundary enforcement

### Step 3: Integrate budget check into execution loop

After each checkpoint-cost write (Step 2 of Task 2), call the budget check:

```python
# After _record_step_cost() returns cumulative_task_cost:
if cumulative_task_cost > 0:
    was_paused = await self._check_budget_and_pause(
        conn, task_data, cumulative_task_cost, worker_id
    )
    if was_paused:
        # Stop execution — task is now paused
        return
```

This must be placed after the checkpoint and cost are durably written, but before the next LangGraph super-step begins. If the task is paused, the executor returns immediately — the worker slot and lease are freed.

### Step 4: Handle edge case — budget change during execution

The design doc specifies: "Budget setting changes on an agent apply to the next checkpoint boundary of already-running tasks." The implementation in Step 1 naturally handles this because `_check_budget_and_pause()` re-reads `budget_max_per_task` and `budget_max_per_hour` from the `agents` table at each checkpoint boundary.

No additional logic is needed — the re-read catches mid-execution budget changes.

## Acceptance Criteria

- [ ] `_check_budget_and_pause()` re-reads agent budget settings at each checkpoint boundary
- [ ] Per-task budget: task pauses when cumulative cost exceeds `budget_max_per_task`
- [ ] Hourly budget: task pauses when rolling 60-minute agent spend exceeds `budget_max_per_hour`
- [ ] Budget precedence: if both limits are exceeded, `budget_per_task` is the pause_reason
- [ ] Budget pause transitions task from `running` to `paused` atomically
- [ ] Budget pause sets `pause_reason`, `pause_details`, and `resume_eligible_at` on the task
- [ ] Budget pause releases the lease (`lease_owner = NULL`, `lease_expiry = NULL`)
- [ ] Budget pause decrements `agent_runtime_state.running_task_count`
- [ ] Budget pause emits `task_paused` event with budget details in `details` JSONB
- [ ] Budget pause is lease-validated (only proceeds if lease_owner matches)
- [ ] After budget pause, executor returns immediately (no further steps)
- [ ] Hourly pause sets `resume_eligible_at` to estimated recovery time
- [ ] Per-task pause sets `resume_eligible_at` to null (requires manual recovery)
- [ ] Mid-execution budget changes are detected at the next checkpoint boundary

## Testing Requirements

- **Unit tests:** Mock task with cumulative cost above per-task budget → verify pause transition with correct reason and details. Mock agent-wide hourly spend above budget → verify hourly pause with resume_eligible_at. Mock both exceeded → verify per-task wins.
- **Integration tests:** Execute a task with low `budget_max_per_task` → verify it pauses after the cost-exceeding step. Verify `agent_runtime_state.running_task_count` is decremented. Verify `task_paused` event has correct details schema.
- **Failure scenarios:** Budget pause when lease already lost → warning log, no crash. Agent deleted mid-execution → check returns False, execution continues.

## Constraints and Guardrails

- Do not implement hourly auto-recovery — Task 5 handles that in the reaper.
- Do not implement the resume API endpoint — Task 6 handles that.
- Do not interrupt in-flight model or tool calls — budget check only runs after checkpoint completion.
- Reuse the existing `_insert_task_event()` helper for event recording.
- The `pause_details` schema is a stable contract consumed by the Console and external automation — match the schema exactly as specified in the design doc.

## Assumptions

- Task 1 has been completed (pause columns exist on `tasks` table).
- Task 2 has been completed (`_record_step_cost()` returns cumulative task cost).
- The `_insert_task_event()` helper exists (added in Track 2's worker interrupt handling).
- The `agents` table has `budget_max_per_task` and `budget_max_per_hour` columns.
- The `agent_cost_ledger` table exists and is populated by Task 2's per-step cost recording.

<!-- AGENT_TASK_END: task-4-budget-enforcement.md -->
