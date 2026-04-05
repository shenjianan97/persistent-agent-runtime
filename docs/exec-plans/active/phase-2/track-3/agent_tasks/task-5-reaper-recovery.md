<!-- AGENT_TASK_START: task-5-reaper-recovery.md -->

# Task 5 — Reaper: Auto-Recovery, Running-Count Reconciliation, and Count Decrements

## Agent Instructions

You are a software engineer implementing one module of a larger system. Your scope is strictly limited to this task. Do not modify components outside the "Affected Component" listed below.

**CRITICAL PRE-WORK:** Before beginning implementation, you MUST read:
1. `docs/design-docs/phase-2/track-3-scheduler-and-budgets.md` — canonical design contract (Hourly auto-recovery, Running-count lifecycle, Running-count reconciliation sections)
2. `services/worker-service/core/reaper.py` — existing reaper scan queries and `run_once()` method
3. `services/worker-service/executor/graph.py` — terminal transition paths: inline completion logic in `run_astream()` (lines 381-461), `_handle_dead_letter()` (lines 747-789), `_handle_retryable_error()` (lines 691-745), and the existing HITL pause transition `_handle_interrupt_internal()` (lines 628-689). Note: there is no named `_handle_completion()` method.
4. `infrastructure/database/migrations/0007_scheduler_and_budgets.sql` — Task 1 output: `agent_runtime_state`, `agent_cost_ledger` schemas

**CRITICAL POST-WORK:** After completing this task:
1. Run the full test suite with `make test` and verify all existing tests still pass. Fix any regressions before proceeding.
2. Update the status in `docs/exec-plans/active/phase-2/track-3/progress.md` to "Done".

## Context

Track 3 introduces three reaper responsibilities and one executor responsibility:

1. **Hourly budget auto-recovery**: Tasks paused for `budget_per_hour` should auto-resume when enough spend ages out of the rolling 60-minute window.
2. **Running-count reconciliation**: Worker crashes can cause `running_task_count` to drift. The reaper periodically corrects this.
3. **Cost ledger pruning**: `agent_cost_ledger` entries older than 2 hours are irrelevant and should be cleaned up.
4. **Running-count decrements on terminal transitions**: Every path that changes a task away from `running` must decrement `running_task_count` transactionally. This includes the existing completion, dead-letter, and reaper requeue paths.

## Task-Specific Shared Contract

- `running_task_count` must be updated transactionally on every path that changes whether a task is `running` (see Running-count lifecycle table in design doc).
- Hourly auto-recovery checks the canonical `agent_cost_ledger`, not the cached `hour_window_cost_microdollars`.
- Auto-recovery respects `max_concurrent_tasks` — only requeues up to available concurrency slots.
- Auto-recovery checks agent status is `active` before transitioning tasks.
- `task_resumed` events carry budget recovery context in `details`.
- `pg_notify('new_task', worker_pool_id)` must be called in the same transaction as auto-recovery transitions.
- Reconciliation recomputes both `running_task_count` and `hour_window_cost_microdollars` from their canonical sources.

## Affected Component

- **Service/Module:** Worker Service — Reaper + Executor terminal paths
- **File paths:**
  - `services/worker-service/core/reaper.py` (modify — add auto-recovery scan, reconciliation scan, ledger pruning)
  - `services/worker-service/executor/graph.py` (modify — add running_task_count decrement to completion and dead-letter paths)
- **Change type:** modification

## Dependencies

- **Must complete first:** Task 1 (Database Migration — tables exist), Task 2 (Incremental Cost — ledger populated), Task 3 (Scheduler Claim — running_task_count incremented on claim)
- **Provides output to:** Task 8 (Integration Tests)
- **Shared interfaces/contracts:** `agent_runtime_state` schema, `agent_cost_ledger` schema, `task_events` event recording

## Implementation Specification

### Step 1: Add running_task_count decrement to executor terminal transitions

In `graph.py`, modify the completion path (`_handle_completion()` or equivalent) to decrement `running_task_count` in the same transaction as the task status change:

```python
# In the completion transaction (after UPDATE tasks SET status='completed'):
await conn.execute(
    '''INSERT INTO agent_runtime_state (tenant_id, agent_id, running_task_count, hour_window_cost_microdollars, scheduler_cursor, updated_at)
       VALUES ($1, $2, 0, 0, '1970-01-01T00:00:00Z', NOW())
       ON CONFLICT (tenant_id, agent_id) DO UPDATE
       SET running_task_count = GREATEST(agent_runtime_state.running_task_count - 1, 0),
           updated_at = NOW()''',
    tenant_id, agent_id
)
```

Apply the same pattern to ALL paths where a running task transitions to a non-running state:
- `_handle_dead_letter()` (running → dead_letter)
- `_handle_retryable_error()` (running → queued) — **this path is easy to miss** but it transitions tasks back to queued with retry backoff, and must decrement the count

The `GREATEST(..., 0)` floor prevents negative counts from reconciliation races.

Use `INSERT ... ON CONFLICT DO UPDATE` to handle agents that don't have a runtime state row yet.

### Step 2: Add running_task_count decrement to reaper requeue path

In `reaper.py`, the `REAPER_REQUEUE_QUERY` transitions tasks from `running` → `queued` when leases expire. Add a `running_task_count` decrement for each requeued task's agent.

After the requeue UPDATE, decrement each affected agent's count:

```python
# After requeue query returns rows:
for row in requeued_rows:
    await conn.execute(
        '''INSERT INTO agent_runtime_state (tenant_id, agent_id, running_task_count, hour_window_cost_microdollars, scheduler_cursor, updated_at)
           VALUES ($1, $2, 0, 0, '1970-01-01T00:00:00Z', NOW())
           ON CONFLICT (tenant_id, agent_id) DO UPDATE
           SET running_task_count = GREATEST(agent_runtime_state.running_task_count - 1, 0),
               updated_at = NOW()''',
        row['tenant_id'], row['agent_id']
    )
```

Similarly, add decrements to:
- The reaper's expired-lease dead-letter path (`REAPER_DEAD_LETTER_QUERY`)
- The reaper's timeout dead-letter path (`REAPER_TIMEOUT_QUERY`) — **this is a separate scan** (lines 74-89 of `reaper.py`) that also transitions `running` → `dead_letter`

**Note:** The existing RETURNING clauses in all three reaper queries (`REAPER_REQUEUE_QUERY`, `REAPER_DEAD_LETTER_QUERY`, `REAPER_TIMEOUT_QUERY`) already include `tenant_id` and `agent_id` — no modifications to the RETURNING clauses are needed.

### Step 3: Add hourly budget auto-recovery scan

Add a new method `_recover_hourly_budget_pauses()` to the reaper, called from `run_once()`:

```python
async def _recover_hourly_budget_pauses(self) -> list[str]:
    """Resume tasks paused for hourly budget once the rolling window clears."""
    recovered_task_ids = []

    # Find agents with hourly-budget-paused tasks whose resume_eligible_at has passed
    paused_agents = await self._pool.fetch(
        '''SELECT DISTINCT t.tenant_id, t.agent_id
           FROM tasks t
           WHERE t.status = 'paused'
             AND t.pause_reason = 'budget_per_hour'
             AND t.resume_eligible_at IS NOT NULL
             AND t.resume_eligible_at <= NOW()'''
    )

    for agent_row in paused_agents:
        tenant_id = agent_row['tenant_id']
        agent_id = agent_row['agent_id']

        async with self._pool.acquire() as conn:
            async with conn.transaction():
                # Verify agent is active
                agent = await conn.fetchrow(
                    '''SELECT budget_max_per_hour, max_concurrent_tasks, status
                       FROM agents WHERE tenant_id = $1 AND agent_id = $2''',
                    tenant_id, agent_id
                )
                if not agent or agent['status'] != 'active':
                    continue

                # Recompute rolling hourly spend from canonical ledger
                hour_cost = await conn.fetchval(
                    '''SELECT COALESCE(SUM(cost_microdollars), 0)
                       FROM agent_cost_ledger
                       WHERE tenant_id = $1 AND agent_id = $2
                         AND created_at > NOW() - INTERVAL '60 minutes' ''',
                    tenant_id, agent_id
                )

                if hour_cost >= agent['budget_max_per_hour']:
                    continue  # Still over budget

                # Get current running count
                runtime = await conn.fetchrow(
                    '''SELECT running_task_count FROM agent_runtime_state
                       WHERE tenant_id = $1 AND agent_id = $2
                       FOR UPDATE''',
                    tenant_id, agent_id
                )
                current_running = runtime['running_task_count'] if runtime else 0
                available_slots = agent['max_concurrent_tasks'] - current_running

                if available_slots <= 0:
                    continue  # No concurrency slots

                # Resume up to available_slots tasks
                resumed_rows = await conn.fetch(
                    '''UPDATE tasks
                       SET status = 'queued',
                           pause_reason = NULL,
                           pause_details = NULL,
                           resume_eligible_at = NULL,
                           version = version + 1,
                           updated_at = NOW()
                       WHERE task_id IN (
                           SELECT task_id FROM tasks
                           WHERE tenant_id = $1 AND agent_id = $2
                             AND status = 'paused'
                             AND pause_reason = 'budget_per_hour'
                           ORDER BY created_at ASC
                           LIMIT $3
                           FOR UPDATE SKIP LOCKED
                       )
                       RETURNING task_id, worker_pool_id''',
                    tenant_id, agent_id, available_slots
                )

                # Record events and notify for each resumed task
                notified_pools = set()
                for row in resumed_rows:
                    await _insert_task_event(
                        conn, str(row['task_id']), tenant_id, agent_id,
                        event_type='task_resumed',
                        status_before='paused',
                        status_after='queued',
                        worker_id=None,
                        details={
                            'resume_trigger': 'automatic_hourly_recovery',
                            'agent_hour_cost_at_resume': hour_cost,
                            'budget_max_per_hour': agent['budget_max_per_hour']
                        }
                    )
                    notified_pools.add(row['worker_pool_id'])
                    recovered_task_ids.append(str(row['task_id']))

                # Notify all affected worker pools
                for pool_id in notified_pools:
                    await conn.execute(
                        "SELECT pg_notify('new_task', $1)", pool_id
                    )

                # Update hourly cost cache while we have the accurate value
                await conn.execute(
                    '''UPDATE agent_runtime_state
                       SET hour_window_cost_microdollars = $1, updated_at = NOW()
                       WHERE tenant_id = $2 AND agent_id = $3''',
                    hour_cost, tenant_id, agent_id
                )

    return recovered_task_ids
```

### Step 4: Add running-count and hourly-cost reconciliation scan

Add `_reconcile_runtime_state()` to the reaper, called from `run_once()`:

```python
async def _reconcile_runtime_state(self) -> int:
    """Reconcile agent_runtime_state with actual task counts and ledger spend."""
    # Reconcile running_task_count
    count_corrections = await self._pool.execute(
        '''UPDATE agent_runtime_state ars
           SET running_task_count = sub.actual_count, updated_at = NOW()
           FROM (
               SELECT tenant_id, agent_id, COUNT(*) AS actual_count
               FROM tasks WHERE status = 'running'
               GROUP BY tenant_id, agent_id
           ) sub
           WHERE ars.tenant_id = sub.tenant_id AND ars.agent_id = sub.agent_id
             AND ars.running_task_count != sub.actual_count'''
    )

    # Also zero out counts for agents with no running tasks but non-zero count
    await self._pool.execute(
        '''UPDATE agent_runtime_state ars
           SET running_task_count = 0, updated_at = NOW()
           WHERE ars.running_task_count > 0
             AND NOT EXISTS (
                 SELECT 1 FROM tasks t
                 WHERE t.tenant_id = ars.tenant_id
                   AND t.agent_id = ars.agent_id
                   AND t.status = 'running'
             )'''
    )

    # Reconcile hour_window_cost_microdollars from canonical ledger
    await self._pool.execute(
        '''UPDATE agent_runtime_state ars
           SET hour_window_cost_microdollars = COALESCE(sub.actual_cost, 0), updated_at = NOW()
           FROM (
               SELECT tenant_id, agent_id, SUM(cost_microdollars) AS actual_cost
               FROM agent_cost_ledger
               WHERE created_at > NOW() - INTERVAL '60 minutes'
               GROUP BY tenant_id, agent_id
           ) sub
           WHERE ars.tenant_id = sub.tenant_id AND ars.agent_id = sub.agent_id
             AND ars.hour_window_cost_microdollars != COALESCE(sub.actual_cost, 0)'''
    )

    return count_corrections
```

### Step 5: Add cost ledger pruning

Add `_prune_cost_ledger()` to the reaper, called from `run_once()`:

```python
async def _prune_cost_ledger(self) -> int:
    """Delete agent_cost_ledger entries older than 2 hours."""
    result = await self._pool.execute(
        '''DELETE FROM agent_cost_ledger
           WHERE created_at < NOW() - INTERVAL '2 hours' '''
    )
    # Parse deleted count from result string
    return int(result.split()[-1]) if result else 0
```

The 2-hour retention provides a safety margin beyond the 60-minute rolling window.

### Step 6: Wire new scans into run_once()

Add calls to the three new methods in the reaper's `run_once()` method:

```python
async def run_once(self) -> dict[str, list[str]]:
    results = {}
    # ... existing scans (requeue, dead-letter, timeout, human-input-timeout, stale workers) ...

    # Track 3: Scheduler and Budget scans
    recovered = await self._recover_hourly_budget_pauses()
    if recovered:
        results['budget_recovered'] = recovered
        logger.info("Auto-recovered %d hourly-budget-paused tasks", len(recovered))

    corrections = await self._reconcile_runtime_state()
    if corrections:
        logger.info("Reconciled %s agent runtime state rows", corrections)

    pruned = await self._prune_cost_ledger()
    if pruned > 0:
        logger.info("Pruned %d old cost ledger entries", pruned)

    return results
```

## Acceptance Criteria

- [ ] Completion path decrements `running_task_count` in the same transaction as status change
- [ ] Dead-letter path decrements `running_task_count` in the same transaction as status change
- [ ] Reaper requeue path decrements `running_task_count` for each requeued task's agent
- [ ] Reaper expired-lease dead-letter path decrements `running_task_count` for each dead-lettered task's agent
- [ ] Reaper timeout dead-letter path decrements `running_task_count` for each timed-out task's agent
- [ ] Executor retryable-error requeue path decrements `running_task_count`
- [ ] All decrements use `GREATEST(..., 0)` floor and `INSERT ... ON CONFLICT DO UPDATE`
- [ ] Hourly auto-recovery: tasks paused for `budget_per_hour` whose `resume_eligible_at <= NOW()` are requeued
- [ ] Auto-recovery recomputes rolling window from `agent_cost_ledger` (not cached state)
- [ ] Auto-recovery checks agent status is `active`
- [ ] Auto-recovery respects `max_concurrent_tasks` — only requeues up to available slots
- [ ] Auto-recovery emits `task_resumed` event with recovery details per task
- [ ] Auto-recovery calls `pg_notify('new_task', worker_pool_id)` in the same transaction
- [ ] Auto-recovery clears `pause_reason`, `pause_details`, `resume_eligible_at` on transitioned tasks
- [ ] Reconciliation corrects `running_task_count` drift on every reaper cycle
- [ ] Reconciliation recomputes `hour_window_cost_microdollars` from canonical ledger
- [ ] Ledger pruning deletes entries older than 2 hours
- [ ] All new scans are called from `run_once()`

## Testing Requirements

- **Unit tests:** Mock paused tasks with `resume_eligible_at` in the past → verify auto-recovery requeues them. Mock agent at max concurrency → verify auto-recovery limits requeue count. Mock running_task_count drift → verify reconciliation corrects it.
- **Integration tests:** Pause a task for hourly budget, advance time past `resume_eligible_at` → verify auto-recovery. Submit tasks, crash worker → verify reconciliation fixes running count. Insert old ledger entries → verify pruning deletes them.
- **Failure scenarios:** Auto-recovery for disabled agent → tasks remain paused. Reconciliation with no drift → no-op. Pruning with no old entries → no-op.

## Constraints and Guardrails

- Do not implement the resume API endpoint — Task 6 handles that.
- Do not modify the claim query — Task 3 handles that.
- Do not implement per-checkpoint cost recording — Task 2 handles that.
- Auto-recovery tasks that remain paused because concurrency slots are full will be picked up in the next reaper cycle.
- The `_insert_task_event()` helper must be available (from Track 2). If the reaper doesn't have it, add a compatible version using direct asyncpg INSERT.
- Reaper scans should be independent — a failure in one scan should not prevent other scans from running.

## Assumptions

- Tasks 1, 2, and 3 have been completed.
- The reaper has access to the database pool via `self._pool` (underscore prefix — see constructor at line 139 of `reaper.py`).
- The existing `_insert_task_event()` is a **module-level function** in `reaper.py` (lines 299-325), NOT an instance method. Call it as `_insert_task_event(conn, ...)` without `self.`.
- The reaper runs on a periodic cycle (existing behavior) — new scans execute on each cycle.
- The reaper's existing RETURNING clauses already include `tenant_id` and `agent_id` — no modifications needed.

<!-- AGENT_TASK_END: task-5-reaper-recovery.md -->
