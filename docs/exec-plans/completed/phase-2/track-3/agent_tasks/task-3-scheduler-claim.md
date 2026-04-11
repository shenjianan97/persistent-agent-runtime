<!-- AGENT_TASK_START: task-3-scheduler-claim.md -->

# Task 3 — Agent-Aware Round-Robin Scheduler Claim

## Agent Instructions

You are a software engineer implementing one module of a larger system. Your scope is strictly limited to this task. Do not modify components outside the "Affected Component" listed below.

**CRITICAL PRE-WORK:** Before beginning implementation, you MUST read:
1. `docs/design-docs/phase-2/track-3-scheduler-and-budgets.md` — canonical design contract (Scheduling Model section, Core Decisions)
2. `services/worker-service/core/poller.py` — current `build_claim_query()` function and `_try_claim()` method
3. `infrastructure/database/migrations/0007_scheduler_and_budgets.sql` — Task 1 output: `agent_runtime_state` schema and indexes

**CRITICAL POST-WORK:** After completing this task:
1. Run the full test suite with `make test` and verify all existing tests still pass. Fix any regressions before proceeding.
2. Update the status in `docs/exec-plans/active/phase-2/track-3/progress.md` to "Done".

## Context

The current claim path uses simple FIFO within each `worker_pool_id`: workers claim the oldest queued task using `FOR UPDATE SKIP LOCKED`. This is insufficient for agent-level fairness — a single hot agent can dominate the queue.

Track 3 replaces the FIFO claim with an agent-aware round-robin scheduler. The claim path changes from "oldest queued task in pool" to "next eligible agent in round-robin order → that agent's oldest queued task in pool".

The scheduler uses `agent_runtime_state` for cheap eligibility checks: concurrency limits (`running_task_count < max_concurrent_tasks`), hourly budget (`hour_window_cost_microdollars < budget_max_per_hour`), and fairness cursor (`scheduler_cursor` — oldest cursor = next served).

## Task-Specific Shared Contract

- Fairness is scoped to `worker_pool_id` — the claim query only considers tasks in the worker's pool.
- Concurrency limits and budgets are evaluated globally per `(tenant_id, agent_id)`.
- The `scheduler_cursor` implements round-robin: set to `NOW()` when an agent's task is claimed, so the agent with the oldest cursor is served next.
- `paused` tasks do NOT count against `max_concurrent_tasks`.
- `SELECT ... FOR UPDATE` on `agent_runtime_state` serializes concurrent claims for the same agent.
- Tasks still use `FOR UPDATE SKIP LOCKED` to prevent claim contention.
- The claim must handle agents without a runtime state row via `INSERT ... ON CONFLICT DO UPDATE`.
- All changes (task status, running_task_count, scheduler_cursor, task_event) happen in one transaction.

## Affected Component

- **Service/Module:** Worker Service — Poller
- **File paths:**
  - `services/worker-service/core/poller.py` (modify — replace `build_claim_query()` and update `_try_claim()`)
- **Change type:** modification

## Dependencies

- **Must complete first:** Task 1 (Database Migration — `agent_runtime_state` table must exist)
- **Provides output to:** Task 4 (Budget Enforcement — relies on correctly maintained running_task_count), Task 5 (Reaper — reconciles running_task_count), Task 8 (Integration Tests)
- **Shared interfaces/contracts:** `agent_runtime_state` schema, atomic running_task_count increments

## Implementation Specification

### Step 1: Replace build_claim_query() with agent-aware round-robin

Replace the existing `build_claim_query()` function with a new implementation that:

1. Finds the next eligible agent in round-robin order for the given worker pool
2. Claims that agent's oldest eligible queued task

The query must be atomic (single transaction) and implement the following logic:

```sql
-- Step 1: Find the next eligible agent (oldest scheduler_cursor among eligible agents)
-- An agent is eligible if:
--   a) agent status is 'active'
--   b) agent has at least one queued task in this pool with no active retry delay
--   c) running_task_count < max_concurrent_tasks
--   d) hour_window_cost_microdollars < budget_max_per_hour

-- Step 2: Lock the agent's runtime state row (FOR UPDATE)

-- Step 3: Claim that agent's oldest eligible queued task (FOR UPDATE SKIP LOCKED)

-- Step 4: Atomically:
--   - Transition task: queued → running, set lease_owner, lease_expiry, version++
--   - Increment agent_runtime_state.running_task_count
--   - Advance agent_runtime_state.scheduler_cursor to NOW()
```

**Important: PostgreSQL does NOT support `FOR UPDATE` inside CTEs.** The claim must be implemented as sequential queries within a single transaction. Do not attempt a CTE-based approach with locking clauses — it will fail at parse time.

**Implementation: sequential queries in one transaction:**

```python
async with conn.transaction():
    # Step 0: Pre-claim upsert — ensure all agents with queued tasks have runtime state rows
    await conn.execute(
        '''INSERT INTO agent_runtime_state (tenant_id, agent_id, running_task_count, hour_window_cost_microdollars, scheduler_cursor, updated_at)
           SELECT DISTINCT t.tenant_id, t.agent_id, 0, 0, '1970-01-01T00:00:00Z', NOW()
           FROM tasks t
           WHERE t.worker_pool_id = $1 AND t.tenant_id = $2 AND t.status = 'queued'
           ON CONFLICT DO NOTHING''',
        worker_pool_id, tenant_id
    )

    # Step 1: Find and lock the next eligible agent (oldest scheduler_cursor)
    # NOTE: Filters by tenant_id to preserve tenant isolation from current claim query
    agent_row = await conn.fetchrow(
        '''SELECT ars.tenant_id, ars.agent_id
           FROM agent_runtime_state ars
           JOIN agents a ON a.tenant_id = ars.tenant_id AND a.agent_id = ars.agent_id
           WHERE a.status = 'active'
             AND ars.tenant_id = $2
             AND ars.running_task_count < a.max_concurrent_tasks
             AND ars.hour_window_cost_microdollars < a.budget_max_per_hour
             AND EXISTS (
                 SELECT 1 FROM tasks t
                 WHERE t.tenant_id = ars.tenant_id
                   AND t.agent_id = ars.agent_id
                   AND t.worker_pool_id = $1
                   AND t.status = 'queued'
                   AND (t.retry_after IS NULL OR t.retry_after < NOW())
             )
           ORDER BY ars.scheduler_cursor ASC
           LIMIT 1
           FOR UPDATE OF ars''',
        worker_pool_id, tenant_id
    )
    if not agent_row:
        return None  # No eligible agents — poller backs off

    # Step 2: Find and lock that agent's oldest eligible queued task
    task_row = await conn.fetchrow(
        '''SELECT task_id FROM tasks
           WHERE tenant_id = $1 AND agent_id = $2
             AND worker_pool_id = $3
             AND status = 'queued'
             AND (retry_after IS NULL OR retry_after < NOW())
           ORDER BY created_at ASC
           LIMIT 1
           FOR UPDATE SKIP LOCKED''',
        agent_row['tenant_id'], agent_row['agent_id'], worker_pool_id
    )
    if not task_row:
        return None  # Task was claimed by another worker

    # Step 3: Claim the task (queued → running)
    claimed = await conn.fetchrow(
        '''UPDATE tasks
           SET status = 'running',
               lease_owner = $1,
               lease_expiry = NOW() + $2 * INTERVAL '1 second',
               version = version + 1,
               updated_at = NOW()
           WHERE task_id = $3
           RETURNING *''',
        worker_id, lease_duration_seconds, task_row['task_id']
    )

    # Step 4: Increment running_task_count and advance scheduler_cursor
    await conn.execute(
        '''UPDATE agent_runtime_state
           SET running_task_count = running_task_count + 1,
               scheduler_cursor = NOW(),
               updated_at = NOW()
           WHERE tenant_id = $1 AND agent_id = $2''',
        agent_row['tenant_id'], agent_row['agent_id']
    )

    # Step 5: Record task_claimed event (same transaction)
    # Use inline INSERT matching the existing poller pattern (poller.py lines 253-261)
    # — there is no _insert_task_event() helper in poller.py
    await conn.execute(
        '''INSERT INTO task_events (tenant_id, task_id, agent_id, event_type,
                                    status_before, status_after, worker_id,
                                    error_code, error_message, details)
           VALUES ($1, $2::uuid, $3, $4, $5, $6, $7, $8, $9, $10::jsonb)''',
        claimed['tenant_id'], str(claimed['task_id']), claimed['agent_id'],
        "task_claimed", "queued", "running", worker_id,
        None, None, "{}"
    )

    return claimed
```

**Key notes on this implementation:**
- `FOR UPDATE OF ars` in Step 1 locks the `agent_runtime_state` row, serializing concurrent claims for the same agent while allowing different agents to proceed in parallel.
- `FOR UPDATE SKIP LOCKED` in Step 2 prevents claim contention on tasks.
- The `retry_after` comparison uses `<` (strict less-than) to match the existing production code behavior.
- The pre-claim upsert (Step 0) ensures newly created agents have runtime state rows — this is the recommended approach over skipping them.

### Step 2: Handle missing agent_runtime_state rows

Agents created after the migration but before their first claim won't have a runtime state row. The pre-claim upsert in Step 0 of the sequential query handles this by ensuring all agents with queued tasks have runtime state rows before the eligibility check runs. This prevents newly created agents from being invisible to the scheduler.

### Step 3: Update _try_claim() to use new query

Update the `_try_claim()` method in the poller to:
1. Call the new sequential claim logic (which replaces the single `build_claim_query()` + `conn.fetchrow()` pattern)
2. The `task_claimed` event is now inserted inside the claim transaction (Step 5 above)
3. Continue to update metrics (active_tasks_count, queue.depth gauge)

**Note on parameter changes:** The current `build_claim_query()` takes only `lease_duration_seconds` as a Python argument; `worker_pool_id`, `tenant_id`, and `worker_id` are SQL bind parameters passed to `conn.fetchrow()`. The new implementation passes these as separate query parameters in the sequential steps. Update the `_try_claim()` call site accordingly.

The existing semaphore check, heartbeat setup, and execution routing remain unchanged.

### Step 4: Ensure tenant_id scoping

The current claim query filters by `tenant_id` from the worker's configuration. Verify this is preserved in the new round-robin query. The `eligible_agent` CTE should include a tenant filter if the worker is tenant-scoped.

If the worker serves all tenants (no tenant filter in current code), the round-robin should work across tenants naturally via the `agent_runtime_state` join.

## Acceptance Criteria

- [ ] `build_claim_query()` returns a round-robin claim query instead of FIFO
- [ ] The claim selects the eligible agent with the oldest `scheduler_cursor` in the pool
- [ ] Only agents with `status = 'active'` are eligible
- [ ] Only agents with `running_task_count < max_concurrent_tasks` are eligible
- [ ] Only agents with `hour_window_cost_microdollars < budget_max_per_hour` are eligible
- [ ] Only agents with at least one queued task (past retry_after) in the pool are eligible
- [ ] The selected agent's oldest queued task in the pool is claimed
- [ ] Task transitions atomically from `queued` to `running` with lease_owner and lease_expiry
- [ ] `agent_runtime_state.running_task_count` is incremented in the same transaction
- [ ] `agent_runtime_state.scheduler_cursor` is advanced to `NOW()` in the same transaction
- [ ] `task_claimed` event is recorded in the same transaction
- [ ] Missing `agent_runtime_state` rows are handled (either pre-created or skipped gracefully)
- [ ] `FOR UPDATE` on `agent_runtime_state` prevents concurrent double-booking for same agent
- [ ] `FOR UPDATE SKIP LOCKED` on tasks prevents claim contention

## Testing Requirements

- **Unit tests:** Mock two agents with queued tasks in the same pool — verify round-robin alternation. Mock an agent at max concurrency — verify it is skipped. Mock an agent over hourly budget — verify it is skipped.
- **Integration tests:** Submit tasks for two agents in the same pool. Claim repeatedly — verify fair alternation. Set `max_concurrent_tasks = 1`, claim one task — verify second claim for same agent is blocked.
- **Failure scenarios:** No eligible agents → claim returns null (poller backs off). Agent without runtime state row → handled gracefully.

## Constraints and Guardrails

- Do not implement budget enforcement at checkpoint boundaries — Task 4 handles that.
- Do not implement running_task_count decrements — Task 5 handles those on terminal transitions.
- Do not change the heartbeat, execution, or completion paths — only the claim query.
- The existing `pg_notify('new_task', worker_pool_id)` listener and exponential backoff fallback remain unchanged.
- Preserve the `task_claimed` event insertion pattern from the current `_try_claim()`.

## Assumptions

- Task 1 has been completed (`agent_runtime_state` table exists with seeded rows for existing agents).
- The worker's `worker_pool_id` is available in the poller configuration (it is — used in current claim query).
- The `agents` table has `max_concurrent_tasks`, `budget_max_per_task`, `budget_max_per_hour` columns (added in Task 1).
- PostgreSQL does NOT support `FOR UPDATE` in CTEs — the implementation uses sequential queries within a single transaction.

<!-- AGENT_TASK_END: task-3-scheduler-claim.md -->
