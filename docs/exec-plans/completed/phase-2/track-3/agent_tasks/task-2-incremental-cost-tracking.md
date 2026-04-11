<!-- AGENT_TASK_START: task-2-incremental-cost-tracking.md -->

# Task 2 — Per-Checkpoint Incremental Cost Tracking

## Agent Instructions

You are a software engineer implementing one module of a larger system. Your scope is strictly limited to this task. Do not modify components outside the "Affected Component" listed below.

**CRITICAL PRE-WORK:** Before beginning implementation, you MUST read:
1. `docs/design-docs/phase-2/track-3-scheduler-and-budgets.md` — canonical design contract (Per-task cumulative cost section, Budget Model section)
2. `services/worker-service/executor/graph.py` — current cost calculation flow: `_get_model_cost_rates()`, `_extract_tokens()`, `_calculate_step_cost()`, and the end-of-task cost aggregation
3. `infrastructure/database/migrations/0007_scheduler_and_budgets.sql` — Task 1 output: `agent_cost_ledger` and `agent_runtime_state` schemas

**CRITICAL POST-WORK:** After completing this task:
1. Run the full test suite with `make test` and verify all existing tests still pass. Fix any regressions before proceeding.
2. Update the status in `docs/exec-plans/active/phase-2/track-3/progress.md` to "Done".

## Context

The existing `GraphExecutor` computes cost only at task completion by iterating all AI messages in the conversation. This is insufficient for Track 3 because budget enforcement must act between LangGraph super-steps (checkpoint boundaries).

Track 3 changes cost tracking from end-of-task aggregation to incremental per-checkpoint tracking. After each LangGraph super-step, the executor writes the step's cost to both:
1. The `checkpoints.cost_microdollars` column (for the individual checkpoint)
2. The `agent_cost_ledger` (for the rolling hourly window)
3. The `agent_runtime_state.hour_window_cost_microdollars` cache (inline update)

This task establishes the cost data pipeline. Budget enforcement logic (checking thresholds and pausing) is Task 4.

## Task-Specific Shared Contract

- Cost values are in microdollars (1 USD = 1,000,000 microdollars).
- Per-checkpoint cost tracking replaces the current end-of-task cost aggregation.
- `agent_cost_ledger` entries are the canonical source for rolling hourly spend.
- `agent_runtime_state.hour_window_cost_microdollars` is updated inline (same transaction as the ledger INSERT) by adding the new entry's cost.
- The existing `_calculate_step_cost()` method computes cost from token usage metadata — reuse this.
- The cumulative per-task cost is derived by summing `cost_microdollars` from `agent_cost_ledger` entries for that `task_id`.
- Checkpoint cost is written per-checkpoint (not only at completion), so `checkpoints.cost_microdollars` reflects individual step cost.

## Affected Component

- **Service/Module:** Worker Service — Executor
- **File paths:**
  - `services/worker-service/executor/graph.py` (modify — add per-step cost recording, replace end-of-task aggregation)
- **Change type:** modification

## Dependencies

- **Must complete first:** Task 1 (Database Migration — `agent_cost_ledger` and `agent_runtime_state` tables must exist)
- **Provides output to:** Task 4 (Budget Enforcement — reads cumulative cost from ledger), Task 5 (Reaper — prunes old ledger entries, recomputes hourly cache)
- **Shared interfaces/contracts:** `agent_cost_ledger` schema, `agent_runtime_state.hour_window_cost_microdollars` cache

## Implementation Specification

### Step 1: Add per-step cost recording method

Add a new method `_record_step_cost()` to `GraphExecutor`:

```python
async def _record_step_cost(
    self,
    conn,
    task_id: str,
    tenant_id: str,
    agent_id: str,
    checkpoint_id: str,
    cost_microdollars: int,
) -> int:
    """Record cost for a single LangGraph super-step.

    Writes to:
    1. checkpoints.cost_microdollars — individual checkpoint cost
    2. agent_cost_ledger — append-only entry for rolling hourly window
    3. agent_runtime_state.hour_window_cost_microdollars — inline cache update

    Returns the cumulative task cost from the ledger.
    """
    # 1. Update the checkpoint's cost
    # NOTE: checkpoints.checkpoint_id is TEXT, not UUID — do not cast
    await conn.execute(
        '''UPDATE checkpoints
           SET cost_microdollars = $1
           WHERE checkpoint_id = $2''',
        cost_microdollars, checkpoint_id
    )

    # 2. Insert into agent_cost_ledger
    # NOTE: agent_cost_ledger.checkpoint_id is TEXT to match checkpoints table
    await conn.execute(
        '''INSERT INTO agent_cost_ledger (tenant_id, agent_id, task_id, checkpoint_id, cost_microdollars)
           VALUES ($1, $2, $3::uuid, $4, $5)''',
        tenant_id, agent_id, task_id, checkpoint_id, cost_microdollars
    )

    # 3. Update the cached hourly cost in agent_runtime_state
    await conn.execute(
        '''INSERT INTO agent_runtime_state (tenant_id, agent_id, running_task_count, hour_window_cost_microdollars, scheduler_cursor, updated_at)
           VALUES ($1, $2, 0, $3, '1970-01-01T00:00:00Z', NOW())
           ON CONFLICT (tenant_id, agent_id) DO UPDATE
           SET hour_window_cost_microdollars = agent_runtime_state.hour_window_cost_microdollars + $3,
               updated_at = NOW()''',
        tenant_id, agent_id, cost_microdollars
    )

    # 4. Return cumulative task cost and hourly cost for budget checks (Task 4 will use both)
    row = await conn.fetchrow(
        '''SELECT COALESCE(SUM(cost_microdollars), 0) AS cumulative_cost
           FROM agent_cost_ledger WHERE task_id = $1::uuid''',
        task_id
    )
    hourly_row = await conn.fetchrow(
        '''SELECT hour_window_cost_microdollars FROM agent_runtime_state
           WHERE tenant_id = $1 AND agent_id = $2''',
        tenant_id, agent_id
    )
    return (row['cumulative_cost'], hourly_row['hour_window_cost_microdollars'] if hourly_row else 0)
```

All five operations must happen within the same transaction. The caller must wrap the call in `async with conn.transaction():` or ensure the connection is already in a transaction block.

### Step 2: Integrate per-step cost recording into the execution loop

The current executor runs the LangGraph graph and writes a checkpoint after each super-step. After each super-step completes and the checkpoint is durably written, calculate and record the step's cost.

Find the execution loop where the graph streams events (the `async for event in graph.astream(...)` loop or equivalent). After each super-step's checkpoint is persisted:

1. Filter streaming events for agent-node outputs only (the `"agent"` key in `astream(stream_mode="updates")` events) — tool-node events do not contain AI messages with cost metadata
2. Extract the new AI message's `response_metadata` from the agent-node event
3. Call `await _calculate_step_cost(response_metadata, model_name)` to compute the step cost — note this is an `async` method that takes a single message's `response_metadata` dict and a `model_name` string (NOT a list of messages)
4. Obtain the `checkpoint_id` for the just-written checkpoint (query the latest checkpoint for this task from the DB, or extract it from the checkpointer's returned config)
5. Call `_record_step_cost()` with the checkpoint ID

```python
# After checkpoint is written for this super-step:
# Filter: only process events from the "agent" node (not "tools" node)
if "agent" in event:
    for ai_msg in event["agent"].get("messages", []):
        if hasattr(ai_msg, 'response_metadata') and ai_msg.response_metadata:
            step_cost, execution_metadata = await self._calculate_step_cost(
                ai_msg.response_metadata, model_name
            )
            if step_cost > 0:
                # Get the checkpoint_id for the just-written checkpoint
                checkpoint_id = await conn.fetchval(
                    '''SELECT checkpoint_id FROM checkpoints
                       WHERE task_id = $1::uuid
                       ORDER BY created_at DESC LIMIT 1''',
                    task_id
                )
                async with conn.transaction():
                    cumulative_task_cost, hourly_cost = await self._record_step_cost(
                        conn, task_id, tenant_id, agent_id, checkpoint_id, step_cost
                    )
```

**Important notes on `_calculate_step_cost()` signature:** The existing method at `graph.py` line 246 has the signature `async def _calculate_step_cost(self, response_metadata: dict, model_name: str) -> tuple[int, dict]`. It accepts a single message's `response_metadata` dict and a model name string — NOT a list of messages or cost-rates tuple. Do not forget the `await`.

### Step 3: Remove end-of-task cost aggregation

The current flow aggregates cost across all AI messages at task completion and writes to the latest checkpoint. This is replaced by per-step recording.

Remove or disable the end-of-task cost aggregation block (the code that iterates all AI messages, sums costs, and updates the latest checkpoint). The per-step writes in Step 2 make this redundant.

The existing `checkpoints.cost_microdollars` query in `TaskRepository.findByIdWithAggregates()` (`SUM(cost_microdollars)` over checkpoints) continues to work correctly because Track 3 writes cost per-checkpoint instead of only at completion.

### Step 4: Handle cost calculation for resumed tasks

When a task resumes from a checkpoint (e.g., after HITL pause or budget pause), the cost recording should only account for new steps, not replay old ones. The `agent_cost_ledger` already ensures this because each entry is keyed by `checkpoint_id` — only new checkpoints produce new ledger entries.

Ensure that `_calculate_step_cost()` is called only for messages produced in the current step, not the full message history. The graph's streaming interface provides per-step outputs — use these rather than diffing the full state.

## Acceptance Criteria

- [ ] `_record_step_cost()` method exists on `GraphExecutor`
- [ ] After each LangGraph super-step, cost is written to `checkpoints.cost_microdollars` for that checkpoint
- [ ] After each LangGraph super-step, an `agent_cost_ledger` entry is created with the step's cost
- [ ] After each LangGraph super-step, `agent_runtime_state.hour_window_cost_microdollars` is incremented by the step's cost
- [ ] All three writes happen in a single transaction
- [ ] `_record_step_cost()` returns a tuple of (cumulative_task_cost, hourly_window_cost) for Task 4 budget enforcement
- [ ] End-of-task cost aggregation is removed or disabled (no longer iterates all AI messages at completion)
- [ ] Per-checkpoint cost is written per step, so `SUM(cost_microdollars)` over checkpoints gives the correct total
- [ ] `agent_runtime_state` rows are created via `INSERT ... ON CONFLICT DO UPDATE` if they don't exist
- [ ] Resumed tasks only record cost for new steps, not replayed history

## Testing Requirements

- **Unit tests:** Mock graph execution with multiple steps — verify each step produces a ledger entry and checkpoint cost update. Verify cumulative cost returned is correct.
- **Integration tests:** Execute a multi-step task, query `agent_cost_ledger` — verify one entry per step. Verify `checkpoints.cost_microdollars` populated per checkpoint (not only at completion). Verify `agent_runtime_state.hour_window_cost_microdollars` reflects the sum.
- **Failure scenarios:** Zero-cost step (no AI messages) should not create a ledger entry. Missing `agent_runtime_state` row should be created via upsert.

## Constraints and Guardrails

- Do not implement budget enforcement logic (threshold checks, pause transitions) — Task 4 handles that.
- Do not implement ledger pruning — Task 5 handles that in the reaper.
- Do not change the `checkpoints` table schema — `cost_microdollars` column already exists.
- Reuse the existing `_calculate_step_cost()` and `_extract_tokens()` methods. Only modify them if necessary to support per-step (vs. per-conversation) cost calculation.
- The `execution_metadata` JSONB field on checkpoints can continue to be written per-step alongside cost.

## Assumptions

- Task 1 has been completed (`agent_cost_ledger` and `agent_runtime_state` tables exist).
- The `checkpoints` table has a `cost_microdollars` column (added in Phase 1).
- The `checkpoints` table has a `checkpoint_id` column of type `TEXT` (not UUID). The `agent_cost_ledger.checkpoint_id` must also be `TEXT` to match.
- LangGraph's streaming interface (`astream(stream_mode="updates")`) provides per-step outputs as dicts keyed by node name (e.g., `"agent"`, `"tools"`). Only `"agent"` node events contain AI messages with `response_metadata` for cost calculation.
- The `checkpoint_id` can be obtained by querying the latest checkpoint for the task after each step, or by inspecting the checkpointer's config after each write.
- The checkpoint write and cost write happen in separate transactions (the checkpointer writes its own transaction internally). If the cost write fails after the checkpoint succeeds, the cost is simply not counted toward budget — this is a fail-safe direction.
- Model cost rates are available via `_get_model_cost_rates()` (queries the `models` table).

<!-- AGENT_TASK_END: task-2-incremental-cost-tracking.md -->
