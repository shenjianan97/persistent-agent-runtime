# Track 3 Design — Scheduler and Budgets

## Context

Track 1 established Agent as a first-class control-plane resource. Track 2 expanded the runtime state model with durable pause states and the append-only `task_events` timeline. Track 3 builds on those foundations to make scheduling agent-aware.

Phase 1 and the current Track 2 runtime still use a simple FIFO claim path within each `worker_pool_id`: workers claim the oldest queued task using `FOR UPDATE SKIP LOCKED`. That is sufficient for durable execution, but it is not sufficient for agent-level fairness or spend control. A single hot agent can dominate the queue, there is no global cap on concurrent work per agent, and budget exhaustion has no runtime effect.

Track 3 introduces the minimum scheduler layer needed for Phase 2:

1. agent-wide concurrency limits
2. fair scheduling across agents within a worker pool
3. budget-based pause behavior
4. operator-facing control over those limits

The goal is not to redesign the queue architecture. PostgreSQL remains the queue and source of truth, workers still claim tasks through the current poller model, and `paused` becomes operational for budget enforcement.

## Goals

- Enforce `max_concurrent_tasks` globally per agent
- Provide fair scheduling within each `worker_pool_id`
- Enforce `budget_max_per_task` and `budget_max_per_hour`
- Pause tasks for budget exhaustion instead of dead-lettering them
- Resume paused tasks safely using durable state transitions
- Extend the Agent control plane so operators can configure concurrency and budget settings
- Surface budget pause state clearly in the API and Console

## Non-Goals

Track 3 does not include:

- queue migration beyond PostgreSQL
- weighted priorities or custom scheduling classes
- per-worker-pool concurrency limits for the same agent
- predictive budget admission based on estimated future step cost (note: claim-time hourly budget blocking is reactive enforcement against observed spend, not predictive admission based on estimated future cost)
- mid-call interruption of in-flight LLM or tool work
- BYOT-specific scheduling rules beyond existing `worker_pool_id` routing
- memory, secret-management, or custom tool runtime work from later tracks
- per-task budget overrides (operators must adjust the agent-level `budget_max_per_task` to resume expensive tasks)
- bulk resume endpoints (single-task resume is sufficient for Track 3 MVP)
- scheduler metrics/counters beyond structured logging (dashboards deferred to a later observability track)

## Core Decisions

- Fair scheduling is evaluated per `worker_pool_id`.
- Concurrency limits and budgets are evaluated globally per `(tenant_id, agent_id)`.
- Fairness policy is round-robin across eligible agents, not global FIFO across tasks.
- Budget enforcement happens only at claim time and checkpoint-cost boundaries. A checkpoint-cost boundary is defined as the completion of a LangGraph super-step — the point at which the checkpointer has durably written the new checkpoint and the executor regains control before starting the next step.
- Crossing a budget never interrupts an in-flight model or tool call mid-step.
- Hourly budget uses a true sliding window: `SUM(cost_microdollars) FROM agent_cost_ledger WHERE created_at > NOW() - INTERVAL '60 minutes'` per `(tenant_id, agent_id)`.
- Hourly budget exhaustion pauses work and auto-recovers once the rolling window clears.
- Per-task budget exhaustion pauses work and requires explicit budget increase plus manual resume.
- The scheduler hot path must not derive running counts and rolling spend by scanning `tasks` and `checkpoints` on every claim.
- `task_paused` and `task_resumed` remain the canonical audit event types for Track 3 pause and resume flows.
- `paused` tasks do NOT count against `max_concurrent_tasks`. The running count decrements on pause and increments again only when the task is re-claimed after resume.
- Budget setting changes on an agent apply to the next checkpoint boundary of already-running tasks, not only to future claims. If an operator lowers `budget_max_per_task`, a running task that exceeds the new limit at its next checkpoint will be paused.

## Data Model

### Agent table extension

Track 3 extends `agents` with the scheduler and budget fields already defined in the Phase 2 overview design.

| Column | Type | Constraints / Meaning |
|--------|------|------------------------|
| `max_concurrent_tasks` | `INT` | NOT NULL, default 5, CHECK > 0, global running-task cap for the agent |
| `budget_max_per_task` | `BIGINT` | NOT NULL, default 500000, CHECK > 0, per-task budget in microdollars |
| `budget_max_per_hour` | `BIGINT` | NOT NULL, default 5000000, CHECK > 0, rolling hourly budget in microdollars |

These are control-plane settings. They are configured on the Agent resource and apply to scheduling and pause decisions for that agent's tasks — including currently running tasks at their next checkpoint boundary.

### Task table extension

Track 3 extends `tasks` with budget pause metadata so paused state is understandable and recoverable.

| Column | Type | Constraints / Meaning |
|--------|------|------------------------|
| `pause_reason` | `TEXT` | nullable, `budget_per_task` or `budget_per_hour` in Track 3 |
| `pause_details` | `JSONB` | nullable, structured budget context for API and UI surfacing |
| `resume_eligible_at` | `TIMESTAMPTZ` | nullable, next known auto-resume time for hourly budget pauses |

These fields remain null for non-paused tasks.

### Scheduler state

Track 3 adds lightweight derived state so claim-time eligibility checks remain cheap and deterministic.

#### `agent_runtime_state`

One row per `(tenant_id, agent_id)`.

| Column | Type | Constraints / Meaning |
|--------|------|------------------------|
| `tenant_id` | `TEXT` | NOT NULL |
| `agent_id` | `TEXT` | NOT NULL |
| `running_task_count` | `INT` | NOT NULL, current global count of running tasks |
| `hour_window_cost_microdollars` | `BIGINT` | NOT NULL, cached rolling hourly spend |
| `scheduler_cursor` | `TIMESTAMPTZ` | NOT NULL, defaults to epoch, fairness cursor: set to `NOW()` when an agent's task is claimed, so the agent with the oldest cursor is served next |
| `updated_at` | `TIMESTAMPTZ` | NOT NULL, defaults to now |

Primary key: `(tenant_id, agent_id)`.

This table is derived operational state, not the source of truth for durable task history.

**Scheduler cursor semantics:** The `scheduler_cursor` implements round-robin by recording when each agent was last served. The claim query selects the eligible agent with the oldest `scheduler_cursor` value, then advances it to `NOW()` after a successful claim. This means the most-recently-served agent goes to the back of the round-robin queue. The cursor is global per agent, not per worker pool — this is intentional because an agent served in pool A should still yield priority to other agents in pool B. If per-pool fairness isolation is needed in the future, the cursor can be moved to a per-pool table.

**Locking strategy:** The claim query must `SELECT ... FOR UPDATE` the `agent_runtime_state` row for the chosen agent. This serializes concurrent claims for the same agent while allowing claims for different agents to proceed in parallel. Tasks still use `FOR UPDATE SKIP LOCKED` to prevent claim contention.

#### `agent_cost_ledger`

Append-only cost entries used to calculate rolling hourly spend without scanning all checkpoints.

| Column | Type | Constraints / Meaning |
|--------|------|------------------------|
| `entry_id` | `UUID` | primary key |
| `tenant_id` | `TEXT` | NOT NULL |
| `agent_id` | `TEXT` | NOT NULL |
| `task_id` | `UUID` | NOT NULL |
| `checkpoint_id` | `UUID` | NOT NULL |
| `cost_microdollars` | `BIGINT` | NOT NULL |
| `created_at` | `TIMESTAMPTZ` | NOT NULL, defaults to now |

**Indexes:** `(tenant_id, agent_id, created_at)` — required for efficient rolling-window queries during claim-time budget checks and reaper recovery scans.

The ledger is the canonical source for rolling hourly spend. `agent_runtime_state.hour_window_cost_microdollars` is the fast-path cache derived from it.

**Cache update:** `hour_window_cost_microdollars` is updated in the same transaction as each `agent_cost_ledger` INSERT, by adding the new entry's cost to the cached value. The reaper periodically recomputes the cache from the canonical ledger to correct any drift.

**Retention:** Entries older than 2 hours are irrelevant for budget enforcement (the rolling window is 60 minutes). A periodic cleanup query — naturally placed in the reaper loop — should delete entries where `created_at < NOW() - INTERVAL '2 hours'`. The 2-hour retention provides a safety margin beyond the 60-minute window.

### Per-task cumulative cost

Per-task budget enforcement requires knowing the cumulative cost of a task without scanning all checkpoints at each boundary. The existing `checkpoints.cost_microdollars` column is only populated once at task completion (end-of-task aggregation in `GraphExecutor`). Track 3 changes this to incremental per-checkpoint cost tracking.

After each LangGraph super-step, the executor writes the step's cost to both:
1. The `checkpoints.cost_microdollars` column (for the individual checkpoint)
2. The `agent_cost_ledger` (for the rolling hourly window)

The cumulative per-task cost is derived by summing `cost_microdollars` from `agent_cost_ledger` entries for that `task_id`. This avoids adding a denormalized column to the `tasks` table while keeping the derivation efficient (the ledger is small — typically a few entries per task within the retention window).

For the API response field `total_cost_microdollars`, the existing `SUM(checkpoints.cost_microdollars)` query in `TaskRepository.findByIdWithAggregates()` continues to work because Track 3 writes cost per-checkpoint instead of only at completion.

### Why Track 3 needs derived scheduler state

The Phase 2 overview already notes that enforcing concurrency directly inside the worker's `FOR UPDATE SKIP LOCKED` claim query can become a database bottleneck under load. The same concern applies to hourly budget checks if they depend on claim-time aggregate scans over `tasks` and `checkpoints`.

Track 3 therefore introduces explicit derived state so the scheduler can decide agent eligibility without turning the claim path into a hot aggregate query.

## Scheduling Model

### Fairness unit

Fairness is scoped to `worker_pool_id` because claiming already happens per pool. Within a pool, the scheduler rotates across eligible agents in round-robin order. For each eligible agent, the scheduler considers that agent's oldest queued task in that pool.

This preserves the current worker-pool routing model while preventing one agent from monopolizing a shared pool.

### Agent eligibility

An agent is eligible for a claim in a given worker pool only if:

- the agent status is `active`
- the agent has at least one queued task in that pool with no active retry delay
- the agent is below `max_concurrent_tasks`
- the agent is not blocked by hourly budget exhaustion

If only one agent is eligible, the scheduler should not artificially idle capacity. Round-robin naturally collapses to that single eligible agent.

### Claim path

The worker claim path changes conceptually from:

- oldest queued task in pool

to:

- next eligible agent in round-robin order for that pool
- then that agent's oldest queued task in that pool

The atomic claim flow must:

1. choose the next eligible agent (oldest `scheduler_cursor` among eligible agents, locked with `FOR UPDATE`)
2. choose that agent's oldest eligible queued task (with `FOR UPDATE SKIP LOCKED`)
3. transition the task to `running`
4. increment `agent_runtime_state.running_task_count`
5. advance `scheduler_cursor` to `NOW()`
6. emit the normal `task_claimed` event

All of that should happen in one transaction.

### Running-count lifecycle

`agent_runtime_state.running_task_count` must be updated transactionally on every path that changes whether a task is `running`:

| Transition | Count change | Where |
|------------|-------------|-------|
| `queued` → `running` (claim) | +1 | Claim query (poller) |
| `running` → `completed` | -1 | Executor completion path |
| `running` → `dead_letter` | -1 | Executor dead-letter path, reaper expired-lease dead-letter |
| `running` → `paused` (budget) | -1 | Executor budget-pause path |
| `running` → `queued` (lease expiry requeue) | -1 | Reaper requeue path |

Each decrement must happen in the same transaction as the task state change. If the `agent_runtime_state` row does not exist (e.g., for tasks created before the Track 3 migration), use `INSERT ... ON CONFLICT DO UPDATE` to initialize it.

### Running-count reconciliation

Worker crashes or unexpected failures can cause `running_task_count` to drift from the true count of `running` tasks. The reaper must include a periodic reconciliation scan:

```sql
UPDATE agent_runtime_state ars
SET running_task_count = sub.actual_count, updated_at = NOW()
FROM (
    SELECT tenant_id, agent_id, COUNT(*) AS actual_count
    FROM tasks WHERE status = 'running'
    GROUP BY tenant_id, agent_id
) sub
WHERE ars.tenant_id = sub.tenant_id AND ars.agent_id = sub.agent_id
  AND ars.running_task_count != sub.actual_count;
```

This runs on every reaper cycle. It is cheap (scans the `tasks` status index) and corrects drift without disrupting normal operation.

### Migration seeding

When migration `0007` runs, existing agents need `agent_runtime_state` rows. The migration seeds them:

```sql
INSERT INTO agent_runtime_state (tenant_id, agent_id, running_task_count, hour_window_cost_microdollars, scheduler_cursor, updated_at)
SELECT a.tenant_id, a.agent_id,
       COALESCE((SELECT COUNT(*) FROM tasks t WHERE t.tenant_id = a.tenant_id AND t.agent_id = a.agent_id AND t.status = 'running'), 0),
       0, EPOCH, NOW()
FROM agents a
ON CONFLICT DO NOTHING;
```

The claim query must also handle agents without a runtime state row (created after migration but before their first claim) via `INSERT ... ON CONFLICT DO UPDATE`.

## Budget Model

Track 3 operationalizes two agent-level limits:

- `budget_max_per_task`: maximum cumulative cost for a single task
- `budget_max_per_hour`: rolling 60-minute cumulative spend cap across all tasks for an agent

All budget values are stored in microdollars.

### Per-task budget semantics

Per-task budget is enforced against the task's cumulative durable execution cost.

A task is not interrupted mid-call when it crosses this threshold. Instead:

1. the current step finishes
2. checkpoint cost is written durably
3. cumulative task cost is compared to `budget_max_per_task`
4. if exceeded, the task transitions from `running` to `paused`

This preserves durable execution guarantees and avoids interrupting partially completed side effects.

### Hourly budget semantics

Hourly budget is enforced against a rolling 60-minute spend window per agent across all worker pools.

Hourly budget is checked in two places:

- before claim, to prevent new work from starting
- after checkpoint-cost write, in case a running task pushes the agent over budget

When hourly budget is exhausted, paused tasks are expected to auto-recover once enough older spend ages out of the rolling window.

### Budget precedence

If a checkpoint boundary reveals that both hourly and per-task limits are exceeded, `budget_per_task` wins as the `pause_reason`. That recovery path is stricter and requires operator action, so it should be the canonical reason persisted on the task.

## Pause and Resume Behavior

### Budget pause

When a running task crosses a budget limit at a checkpoint boundary, the runtime must:

- transition the task from `running` to `paused`
- release the lease
- persist `pause_reason`, `pause_details`, and `resume_eligible_at` when applicable
- emit `task_paused`

This uses the pause state introduced in Track 2 rather than dead-lettering the task.

### Hourly auto-recovery

Hourly budget pauses recover automatically. A recovery scan, most naturally in the existing reaper loop, should:

1. find tasks paused for `budget_per_hour` whose `resume_eligible_at <= NOW()`
2. group by `(tenant_id, agent_id)` to avoid N+1 queries — recompute each agent's rolling window once
3. verify the agent status is `active` (do not auto-resume tasks for disabled agents)
4. recompute whether the rolling 60-minute window is now below `budget_max_per_hour`
5. if eligible, transition **up to `max_concurrent_tasks` minus current `running_task_count`** tasks to `queued` (this prevents thundering herd — if 20 tasks are paused but only 5 slots are available, only 5 are requeued)
6. clear pause metadata on transitioned tasks
7. emit `task_resumed` for each transitioned task
8. call `pg_notify('new_task', worker_pool_id)` in the same transaction

Tasks that remain paused because concurrency slots are full will be picked up in the next reaper cycle once running tasks complete and free slots.

### Manual recovery for per-task pauses

Per-task budget pauses do not auto-recover, because time alone does not reduce cumulative task cost.

Recovery requires:

1. operator increases the agent's `budget_max_per_task`
2. operator explicitly resumes the task
3. resume logic verifies the new budget now allows continued execution
4. the task transitions back to `queued`

## API Design

Track 3 extends the existing Agent and Task APIs. It does not add a new top-level scheduler resource.

### Agent APIs

`POST /v1/agents`, `GET /v1/agents/{agent_id}`, `PUT /v1/agents/{agent_id}`, and `GET /v1/agents` should expose the new scheduler fields:

- `max_concurrent_tasks`
- `budget_max_per_task`
- `budget_max_per_hour`

These fields behave like the rest of the Agent resource: updates affect future scheduling and pause decisions, not historical task snapshots.

Agent detail responses should include the new fields alongside the existing config payload:

```json
{
  "agent_id": "support-agent-v1",
  "display_name": "Support Agent",
  "agent_config": {
    "system_prompt": "You are a helpful support assistant.",
    "provider": "anthropic",
    "model": "claude-3-5-sonnet-latest",
    "temperature": 0.7,
    "allowed_tools": ["web_search", "read_url"]
  },
  "max_concurrent_tasks": 5,
  "budget_max_per_task": 500000,
  "budget_max_per_hour": 5000000,
  "status": "active",
  "created_at": "2026-04-04T10:00:00Z",
  "updated_at": "2026-04-04T10:00:00Z"
}
```

### Task APIs

Task detail responses should expose enough information to make `paused` meaningful for budget scenarios:

- `pause_reason`
- `pause_details`
- `resume_eligible_at`

Task summary responses (list view) should include `pause_reason` and `resume_eligible_at` so operators can distinguish pause types without N+1 detail fetches.

The task list endpoint (`GET /v1/tasks`) should accept `pause_reason` as an optional filter parameter alongside the existing `status` and `agent_id` filters. This enables queries like `GET /v1/tasks?status=paused&pause_reason=budget_per_task` for operational dashboards.

For hourly pauses, `pause_details` should indicate automatic recovery. For per-task pauses, `pause_details` should indicate manual recovery after budget increase.

Example paused task detail response:

```json
{
  "task_id": "2a6f3f0c-7d7d-4f1a-a8f4-2f72f6c7e4ae",
  "agent_id": "support-agent-v1",
  "agent_display_name": "Support Agent",
  "status": "paused",
  "total_cost_microdollars": 534000,
  "pause_reason": "budget_per_task",
  "pause_details": {
    "budget_max_per_task": 500000,
    "observed_task_cost_microdollars": 534000,
    "recovery_mode": "manual_resume_after_budget_increase"
  },
  "resume_eligible_at": null
}
```

### `pause_details` schema

The `pause_details` JSONB field has a defined schema per `pause_reason`:

**For `budget_per_task`:**
```json
{
  "budget_max_per_task": 500000,
  "observed_task_cost_microdollars": 534000,
  "recovery_mode": "manual_resume_after_budget_increase"
}
```

**For `budget_per_hour`:**
```json
{
  "budget_max_per_hour": 5000000,
  "observed_hour_cost_microdollars": 5120000,
  "recovery_mode": "automatic_after_window_clears"
}
```

These keys are stable contracts consumed by the Console and external automation. Changes to the schema require a version bump or additive-only evolution.

### Resume endpoint

Track 3 adds:

- `POST /v1/tasks/{task_id}/resume`

This endpoint is valid only for `paused` tasks that are manually resumable. In Track 3, that means per-task budget pauses. The endpoint takes no request body.

Behavior:

- reject with 409 if the task is not paused
- reject with 409 if the task is still above the effective per-task budget (re-read `budget_max_per_task` from the agent at resume time)
- reject with 409 if the agent is disabled
- on success, transition the task to `queued`
- clear `pause_reason`, `pause_details`, `resume_eligible_at`
- emit `task_resumed` with details including the new budget limit
- call `pg_notify('new_task', worker_pool_id)` in the same transaction
- return `RedriveResponse` (same shape as approve/reject/respond: `{ task_id, status, message }`)

The resume endpoint is idempotent: if the task has already been resumed and is now `queued`, return 409 with a descriptive message rather than erroring.

Resumed tasks resume from the last LangGraph checkpoint, consistent with the stateless resume model from Track 2 (the same worker or a different worker can claim and continue).

## Console Design

Track 3 extends the existing Agent and Task surfaces rather than introducing a separate scheduler page.

### Agents area

The Agent detail page should add editable fields for:

- max concurrent tasks
- max budget per task
- max budget per hour

These fields belong with the rest of the agent's reusable runtime configuration, because they are long-lived control-plane settings rather than task-scoped overrides.

The Agents list should include `max_concurrent_tasks`, `budget_max_per_task`, and `budget_max_per_hour` in the `AgentSummaryResponse` so operators can compare scheduling policies across agents without opening each detail page.

### Task views

Task list, task detail, and related operational views should show:

- whether pause is due to hourly or per-task budget
- whether recovery is automatic or manual
- the next eligible resume time for hourly pauses when known
- a Resume action only when the task is manually resumable (conditional on `pause_reason === 'budget_per_task'`)

The `TaskStatusBadge` currently shows a generic "Paused" label for all paused tasks. Track 3 should enhance it to distinguish budget-paused tasks from HITL-paused tasks (future), showing the `pause_reason` as a sub-label or tooltip.

The existing task events timeline should show:

- `task_paused` with budget details in the event metadata
- `task_resumed` with recovery context
- later `task_claimed` after resume

No new scheduler-specific Console page is required in this track.

## Observability and Events

Track 3 uses the `task_events` timeline introduced in Track 2.

Required event behavior:

- `task_paused` for budget-triggered pause transitions
- `task_resumed` for both automatic and manual recovery

### Event `details` schema

The `details` JSONB field on `task_events` carries budget context for pause and resume events.

**`task_paused` details (budget_per_task):**
```json
{
  "pause_reason": "budget_per_task",
  "budget_max_per_task": 500000,
  "observed_task_cost_microdollars": 534000,
  "recovery_mode": "manual_resume_after_budget_increase"
}
```

**`task_paused` details (budget_per_hour):**
```json
{
  "pause_reason": "budget_per_hour",
  "budget_max_per_hour": 5000000,
  "observed_hour_cost_microdollars": 5120000,
  "recovery_mode": "automatic_after_window_clears",
  "resume_eligible_at": "2026-04-04T11:15:00Z"
}
```

**`task_resumed` details:**
```json
{
  "resume_trigger": "automatic_hourly_recovery",
  "agent_hour_cost_at_resume": 4200000,
  "budget_max_per_hour": 5000000
}
```
or:
```json
{
  "resume_trigger": "manual_operator_resume",
  "budget_max_per_task_at_resume": 1000000,
  "task_cost_microdollars": 534000
}
```

These schemas are consumed by the Console's `CheckpointTimeline` component (which already reads `details.reason`, `details.message`, `details.prompt` from HITL events) and should be treated as stable contracts.

Structured logging should include `agent_id`, `task_id`, `pause_reason`, and relevant budget values for pause and resume transitions.

## Risks and Open Questions

| Risk | Mitigation |
|------|-----------|
| Claim-time fairness logic could become complex and fragile | Keep fairness scoped to the existing `worker_pool_id` boundary; use explicit derived scheduler state; `FOR UPDATE` on `agent_runtime_state` prevents double-booking |
| Running counts could drift from real task state | Transactional updates on every claim/terminal/pause path + periodic reaper reconciliation scan |
| Hourly cached spend could become stale | `agent_cost_ledger` is the canonical source; reaper recomputes during recovery scans; cache updated inline on cost writes |
| Budget pauses could be confusing to operators | Expose clear `pause_reason`, `pause_details` (with documented schema), `resume_eligible_at`, and task timeline events |
| Resume could requeue work that is still invalid | Resume endpoint re-reads `budget_max_per_task` from agent and revalidates before transitioning to `queued` |
| `agent_cost_ledger` unbounded growth | Reaper prunes entries older than 2 hours on each cycle |
| Hourly auto-recovery thundering herd | Recovery scan respects `max_concurrent_tasks` — only requeues tasks up to available concurrency slots |
| Hourly auto-recovery for disabled agents | Recovery scan checks agent status is `active` before transitioning tasks |
| Worker crash leaves inflated `running_task_count` | Reaper reconciliation scan corrects drift on every cycle |
| Per-checkpoint cost tracking changes the executor hot path | Implemented incrementally: per-checkpoint cost write is independent of budget enforcement |
| Backward compatibility during migration | `agent_runtime_state` seeded from existing agents; claim query handles missing rows via `INSERT ... ON CONFLICT` |

## Testing Strategy

### Database / query verification

Add canonical verification coverage for:

- scheduler state initialization
- fair claim selection across agents
- transactional running-count updates
- budget pause transitions
- hourly auto-resume transitions

### Backend integration

Add end-to-end tests for:

- fairness across multiple agents in one worker pool
- global concurrency cap across multiple workers
- hourly budget claim blocking
- hourly budget pause after checkpoint write
- hourly auto-resume after spend ages out of the rolling window
- per-task budget pause after checkpoint write
- failed manual resume while still over budget
- successful manual resume after budget increase
- correct `task_events` ordering around pause and resume

### Console

Add tests for:

- Agent detail form fields for concurrency and budgets
- paused-task rendering in task detail and list views
- Resume action visibility and behavior for manually resumable budget pauses
