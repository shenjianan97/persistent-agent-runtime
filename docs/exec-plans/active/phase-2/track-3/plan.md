# Phase 2 Track 3 — Scheduler and Budgets: Orchestrator Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add agent-aware fair scheduling, concurrency limits, and budget-based pause/resume to the persistent agent runtime.

**Architecture:** Workers claim tasks via a round-robin scheduler that rotates across eligible agents per worker pool. Concurrency and budget limits are enforced through derived `agent_runtime_state` and append-only `agent_cost_ledger` tables. Budget exhaustion pauses tasks using Track 2's durable pause state; hourly pauses auto-recover via the reaper loop, per-task pauses require operator action.

**Tech Stack:** PostgreSQL (scheduling state, cost ledger), Spring Boot (API extensions), Python asyncpg (worker claim/checkpoint path), React/TypeScript (console)

---

## A1. Implementation Overview

Track 3 extends the Phase 1/2 runtime with:
1. Database schema for scheduler state (`agent_runtime_state`), cost ledger (`agent_cost_ledger`), agent budget columns, and task pause metadata
2. Per-checkpoint incremental cost tracking in the worker executor (replacing end-of-task aggregation)
3. Agent-aware round-robin claim query replacing the current FIFO claim
4. Budget enforcement at checkpoint boundaries with pause transitions
5. Hourly auto-recovery in the reaper loop
6. Agent API extensions for concurrency/budget settings
7. Task resume API endpoint for per-task budget pauses
8. Console updates for budget fields and pause state rendering
9. Integration tests for the full scheduler + budget flow

**Canonical design contract:** `docs/design-docs/phase-2/track-3-scheduler-and-budgets.md`

---

## A2. Impacted Components

| Component | Path | Change Type | Description |
|-----------|------|-------------|-------------|
| Database Schema | `infrastructure/database/migrations/` | new migration | `0007_scheduler_and_budgets.sql`: agent budget columns, task pause columns, `agent_runtime_state`, `agent_cost_ledger` |
| Worker Poller | `services/worker-service/core/poller.py` | modification | Replace FIFO claim with agent-aware round-robin claim query |
| Worker Executor | `services/worker-service/executor/graph.py` | modification | Per-checkpoint cost tracking, budget enforcement at checkpoint boundaries, pause transition |
| Worker Reaper | `services/worker-service/core/reaper.py` | modification | Hourly budget auto-recovery scan, running-count reconciliation |
| Agent API | `services/api-service/` | modification | Budget/concurrency fields on Agent CRUD, new scheduler fields in responses |
| Task Resume API | `services/api-service/` | new code | `POST /v1/tasks/{task_id}/resume` endpoint |
| Console | `services/console/src/` | modification | Agent budget form fields, task pause state rendering, resume action |
| Integration Tests | `tests/backend-integration/` | new code | Scheduler, budget, pause/resume E2E tests |

---

## A3. Dependency Graph

```
Task 1 (Schema) ─┬──→ Task 2 (Incremental Cost) ──→ Task 4 (Budget Enforcement) ──┐
                  │                                                                  │
                  ├──→ Task 3 (Scheduler Claim) ────────────────────────────────────→├──→ Task 8 (Integration Tests)
                  │                                                                  │
                  ├──→ Task 5 (Reaper: Auto-Recovery + Reconciliation) ─────────────→│
                  │                                                                  │
                  ├──→ Task 6 (Agent + Task API) ──→ Task 7 (Console) ──────────────→│
                  │                                                                  │
                  └──→ Task 6 (includes Resume endpoint)                             │
```

**Parallelization opportunities:**
- After Task 1: Tasks 2, 3, 5, 6 can all start in parallel
- Task 4 depends on Task 2 (incremental cost must exist before budget enforcement)
- Task 7 depends on Task 6 (API must expose fields before console can consume them)
- Task 8 depends on all backend tasks (1-6)

---

## A4. Data / API / Schema Changes

**Agent table extension:** Non-breaking. Three new columns with defaults. Existing agents get sensible defaults.

**Task table extension:** Non-breaking. Three new nullable columns for pause metadata.

**New tables (`agent_runtime_state`, `agent_cost_ledger`):** Additive. No existing data affected.

**Agent API:** Backward compatible — new optional fields on create/update, new fields on responses.

**Task API:** Backward compatible — new nullable fields on task responses. New `POST /v1/tasks/{id}/resume` endpoint.

**Claim query:** Breaking change to worker-side SQL. Must deploy migration before new worker.

---

## A4.1. Task Handoff Outputs

| Task | Output |
|------|--------|
| Task 1 | Migration `0007_scheduler_and_budgets.sql` with agent columns, task columns, `agent_runtime_state`, `agent_cost_ledger`, indexes, seeding |
| Task 2 | Per-checkpoint cost writes to `agent_cost_ledger`, `_record_step_cost()` method in executor |
| Task 3 | Agent-aware round-robin `build_claim_query()`, atomic `running_task_count` increment, fairness cursor |
| Task 4 | `_check_budget_and_pause()` in executor, per-task and hourly budget transitions from `running` to `paused` |
| Task 5 | Reaper hourly auto-recovery scan, `running_task_count` reconciliation, `running_task_count` decrement on terminal transitions |
| Task 6 | Agent CRUD with budget/concurrency fields, task responses with pause metadata, `POST /v1/tasks/{id}/resume` |
| Task 7 | Console: agent budget form fields, task pause state badges, resume button |
| Task 8 | Integration tests for fair scheduling, concurrency caps, budget pauses, auto-recovery, manual resume |

---

## A5. Integration Points

| Caller | Callee | Interface Change | Failure Handling |
|--------|--------|-------------------|-----------------|
| Worker Poller | PostgreSQL `agent_runtime_state` | New round-robin claim query joins `agent_runtime_state`, increments `running_task_count`, advances `scheduler_cursor` | `SELECT ... FOR UPDATE` on `agent_runtime_state` prevents double-booking; `SKIP LOCKED` on tasks prevents claim contention |
| Worker Executor | PostgreSQL `agent_cost_ledger` | INSERT per-checkpoint cost entry after each LLM step | INSERT in same transaction as checkpoint write |
| Worker Executor | PostgreSQL `tasks` | Budget-pause transition: `running` → `paused` with `pause_reason`, `pause_details`, `resume_eligible_at` | Lease validation before pause; follows existing terminal-transition pattern |
| Worker Reaper | PostgreSQL `agent_runtime_state` | Periodic reconciliation + hourly auto-recovery scan | UPDATE ... RETURNING pattern consistent with existing reaper queries |
| API Service | PostgreSQL `agents` | Extended SELECT/INSERT/UPDATE with budget columns | Validation on create/update; defaults on missing fields |
| API Service | PostgreSQL `tasks` | Resume mutation: `paused` → `queued` with budget revalidation | CTE + MutationResult pattern; rejects if still over budget |
| Console | API `/v1/agents` | New fields in request/response | Backward compatible rendering |
| Console | API `/v1/tasks/{id}/resume` | New POST action | Error toast on failure; button conditional on pause_reason |

---

## A6. Deployment and Rollout

Same pattern as Tracks 1 and 2: single coordinated deployment. Migration `0007` is picked up by the schema-bootstrap ledger.

**Deployment order:** Database migration MUST run before new worker code deploys. The new claim query references `agent_runtime_state` which must exist. The old claim query continues to work on the new schema.

**Migration seeding:** Existing agents get `agent_runtime_state` rows seeded in the migration via `INSERT INTO agent_runtime_state SELECT ... FROM agents`. Running task counts are initialized from a count of currently running tasks.

**For local development:** `make db-reset` applies all migrations including `0007`.

---

## A7. Observability

- `agent_cost_ledger` provides per-step cost audit trail (replacing end-of-task aggregation)
- `task_paused` and `task_resumed` events carry budget metadata in `details` JSONB
- Budget pause events include: `pause_reason`, `budget_max`, `observed_cost`, `recovery_mode`
- Structured logging on pause/resume transitions includes `agent_id`, `task_id`, `pause_reason`, and budget values
- Reaper logs auto-recovery transitions and reconciliation corrections

---

## A8. Risks and Open Questions

| Risk | Mitigation |
|------|-----------|
| Claim-time fairness logic could become complex and fragile | Keep fairness scoped to `worker_pool_id`; use explicit derived state in `agent_runtime_state` |
| Running counts could drift from real task state | Transactional updates on claim, terminal, and pause + periodic reaper reconciliation |
| Round-robin cursor contention under concurrent workers | `FOR UPDATE` on `agent_runtime_state` serializes per-agent; different agents don't contend |
| Per-checkpoint cost tracking changes the executor's hot path | Incremental: add cost write first (Task 2), then enforcement (Task 4); each independently testable |
| Hourly cached spend could become stale | `agent_cost_ledger` is canonical; reaper recomputes on recovery scans |
| Budget pauses could be confusing to operators | Expose clear `pause_reason`, `pause_details`, `resume_eligible_at`, and task timeline events |
| Resume could requeue work that is still invalid | Resume endpoint revalidates budget and agent status before transitioning to `queued` |
| `agent_cost_ledger` unbounded growth | Add partial index and document pruning as tech-debt item (entries > 2 hours old irrelevant for enforcement) |

---

## A9. Orchestrator Guidance

- Use `docs/design-docs/phase-2/track-3-scheduler-and-budgets.md` as the canonical design contract
- Task 1 must land first. Tasks 2, 3, 5, 6 can proceed in parallel after Task 1
- The existing `CancelResult` / `HitlMutationResult` pattern in `TaskRepository` is the direct template for the resume mutation
- Worker-side pause uses the same lease-validated `UPDATE ... RETURNING` pattern as completion and dead-letter
- All `running_task_count` mutations must happen inside the same transaction as the paired task-state change
- Budget values are stored in microdollars (1 USD = 1,000,000 microdollars) — the same unit as `checkpoints.cost_microdollars`
- The `scheduler_cursor` is a `TIMESTAMPTZ` set to `NOW()` when an agent is served, implementing round-robin by always selecting the eligible agent with the oldest cursor
- Checkpoint-cost boundary = after each LangGraph super-step completes and the checkpoint is durably written
- `paused` tasks do NOT count against `max_concurrent_tasks` — the count decrements on pause and increments again only if/when the task is re-claimed after resume
- Hourly budget uses a true sliding window: `SUM(cost_microdollars) FROM agent_cost_ledger WHERE created_at > NOW() - INTERVAL '60 minutes'`
- Do not add per-task budget overrides — Track 3 uses agent-level settings only
- Do not add bulk resume — single-task resume is sufficient for Track 3 MVP
- Do not add a separate scheduler API resource — extend existing Agent and Task APIs

---

## A10. Key Design Decisions

1. **Derived scheduler state, not claim-time aggregates** — `agent_runtime_state` caches `running_task_count` and `hour_window_cost_microdollars` so the claim query doesn't scan `tasks` and `checkpoints`.

2. **Per-checkpoint cost tracking replaces end-of-task aggregation** — The existing `GraphExecutor` computes cost only at completion by iterating all AI messages. Track 3 writes cost incrementally per LangGraph super-step so budget enforcement can act between steps.

3. **Round-robin via cursor timestamp** — The `scheduler_cursor` on `agent_runtime_state` is set to `NOW()` when an agent's task is claimed. The claim query selects the eligible agent with the oldest cursor, naturally implementing round-robin.

4. **Budget pause reuses Track 2's `paused` state** — No new status values needed. `pause_reason` and `pause_details` distinguish budget pauses from future pause types.

5. **Hourly auto-recovery in the reaper** — Naturally extends the existing reaper scan pattern. Recovery checks rolling window from `agent_cost_ledger`, not cached state.

6. **Running-count reconciliation in the reaper** — Periodic `SELECT COUNT(*) FROM tasks WHERE status='running' AND agent_id=...` catches drift from crashes.

7. **Locking strategy** — `SELECT ... FOR UPDATE` on the `agent_runtime_state` row during claim serializes per-agent decisions. Different agents don't contend. Tasks still use `FOR UPDATE SKIP LOCKED`.

---

## B. Agent Task Files

| Task | File | Description |
|------|------|-------------|
| Task 1 | [task-1-database-migration.md](agent_tasks/task-1-database-migration.md) | Schema: agent budget columns, task pause columns, scheduler state tables, indexes |
| Task 2 | [task-2-incremental-cost-tracking.md](agent_tasks/task-2-incremental-cost-tracking.md) | Per-checkpoint cost writes to `agent_cost_ledger` |
| Task 3 | [task-3-scheduler-claim.md](agent_tasks/task-3-scheduler-claim.md) | Agent-aware round-robin claim query |
| Task 4 | [task-4-budget-enforcement.md](agent_tasks/task-4-budget-enforcement.md) | Budget check + pause at checkpoint boundaries |
| Task 5 | [task-5-reaper-recovery.md](agent_tasks/task-5-reaper-recovery.md) | Hourly auto-recovery, running-count reconciliation, count decrements |
| Task 6 | [task-6-api-extensions.md](agent_tasks/task-6-api-extensions.md) | Agent budget fields, task pause fields, resume endpoint |
| Task 7 | [task-7-console-updates.md](agent_tasks/task-7-console-updates.md) | Agent budget form, task pause rendering, resume action |
| Task 8 | [task-8-integration-tests.md](agent_tasks/task-8-integration-tests.md) | E2E tests for scheduler, budgets, pause/resume |
