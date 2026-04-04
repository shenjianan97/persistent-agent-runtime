# Phase 2 Track 2 — Runtime State Model: Orchestrator Plan

## A1. Implementation Overview

Track 2 extends the Phase 1 task lifecycle with:
1. Three new durable pause statuses: `waiting_for_approval`, `waiting_for_input`, `paused`
2. Append-only `task_events` table for lifecycle audit history
3. Human-in-the-loop API endpoints: approve, reject, respond
4. Worker-side LangGraph `interrupt()` handling for pause state transitions
5. Reaper-side human-input-timeout enforcement
6. Console updates: new status badges, approval/input panels, events timeline
7. Integration tests for the full HITL + events flow

**Canonical design contract:** `docs/design-docs/phase-2/design.md` (Sections 5, 7, 8)

---

## A2. Impacted Components

| Component | Path | Change Type | Description |
|-----------|------|-------------|-------------|
| Database Schema | `infrastructure/database/migrations/` | new migration | `0006_runtime_state_model.sql`: status expansion, task_events table, new task columns |
| Event Service | `services/api-service/` | new code | TaskEventRepository, TaskEventService, GET events endpoint |
| HITL API | `services/api-service/` | modification + new | Approve/reject/respond endpoints, cancel expansion |
| Worker Executor | `services/worker-service/executor/` | modification | GraphInterrupt handling, interrupt-to-pause transition |
| Worker Reaper | `services/worker-service/core/` | modification | Human-input-timeout scan |
| Console | `services/console/src/` | modification + new | Status types, badges, approval/input panels, events timeline |
| Integration Tests | `tests/backend-integration/` | new code | HITL flow tests, event sequence tests |

---

## A3. Dependency Graph

```
Task 1 (Schema) ─┬──→ Task 2 (Event Service) ──┬──→ Task 5 (Event Integration)
                  │                              │
                  ├──→ Task 3 (HITL API) ────────┤──→ Task 4 (Worker Interrupt) ──→ Task 7 (Integration Tests)
                  │                              │
                  └──→ Task 6 (Console) ←────────┘
```

**Parallelization opportunities:**
- After Task 1: Tasks 2, 3 can run in parallel
- After Tasks 2+3: Tasks 4, 5, 6 can run in parallel
- Task 7 waits for all backend tasks (1-5)

---

## A4. Data / API / Schema Changes

**Status expansion:** Non-breaking. Existing statuses unchanged. New statuses added to CHECK constraint.

**New table (`task_events`):** Additive. No existing data affected.

**New task columns:** All nullable. Existing tasks unaffected.

**New API endpoints:** Additive. No existing endpoint signatures change.

**Cancel expansion:** `cancelTask()` now accepts `waiting_for_approval`, `waiting_for_input`, `paused` as valid source states in addition to `queued` and `running`.

---

## A4.1. Task Handoff Outputs

| Task | Output |
|------|--------|
| Task 1 | Migration `0006_runtime_state_model.sql` with expanded status CHECK, task_events table, new task columns, indexes |
| Task 2 | `TaskEventRepository`, `TaskEventService`, `TaskEventResponse` records, `GET /v1/tasks/{id}/events` endpoint |
| Task 3 | `POST approve/reject/respond` endpoints, updated cancel logic, updated task status responses with HITL fields |
| Task 4 | Worker `GraphInterrupt` handling, `_handle_interrupt()`, `request_human_input` tool, reaper timeout scan |
| Task 5 | Event emission from all API-side, worker-side, and reaper-side state transitions |
| Task 6 | Console: new status badges, `ApprovalPanel`, `InputResponsePanel`, `TaskEventsTimeline` |
| Task 7 | Integration tests for approval, input, timeout, and event sequence flows |

---

## A5. Integration Points

| Caller | Callee | Interface Change | Failure Handling |
|--------|--------|-------------------|-----------------|
| API Service | PostgreSQL `task_events` | New INSERT/SELECT queries for event recording | Event INSERT participates in the same transaction as the paired task-state mutation; INSERT failure rolls back the operation |
| API Service | PostgreSQL `tasks` | Approve/reject/respond persist HITL resume payloads, transition waiting tasks back to `queued`, and reuse the existing `new_task` notification path; expanded cancel | 404 missing task, 409 wrong state (CTE + MutationResult pattern) |
| Worker | PostgreSQL `tasks` | Interrupt-to-pause atomic UPDATE releases the lease; resumed workers read and decode `human_response` after a normal claim | Lease validation before pause transition; resumed execution follows the standard claim + heartbeat path |
| Worker | PostgreSQL `task_events` | Direct asyncpg INSERT for worker-side events | INSERT occurs in the same connection/transaction as the paired worker or reaper state transition |
| Console | API `/v1/tasks/{id}/approve\|reject\|respond` | New POST calls from approval/input panels | Error toast on failure; form validation |
| Console | API `/v1/tasks/{id}/events` | New GET call for events timeline | Empty state when no events |

---

## A6. Deployment and Rollout

Same pattern as Track 1: single coordinated deployment. Migration `0006` is picked up by the schema-bootstrap ledger. All changes are additive — no breaking contract changes. The new pause states are only reachable via the new worker interrupt path, so existing tasks continue to work normally during rollout.

**Migration:** `0006_runtime_state_model.sql` follows the `^\d{4}_.*\.sql$` naming convention and is tracked in the `schema_migrations` ledger. Do not apply manually.

**For local development:** `make db-reset` applies all migrations including `0006`.

---

## A7. Observability

- `task_events` table provides the audit trail that was missing in Phase 1
- Events endpoint enables Console timeline rendering
- Reaper logs human-input-timeout dead-letters with `human_input_timeout` reason
- Existing structured logging covers new API endpoints via Spring Boot request logging

---

## A8. Risks and Open Questions

| Risk | Mitigation |
|------|-----------|
| LangGraph `interrupt()` behavior may differ across versions | Verify with langgraph 1.0.5 (current). The `GraphInterrupt` exception is the stable API |
| Resume after approval may re-execute the interrupted node | LangGraph checkpoint stores pre-interrupt state. `Command(resume=...)` provides the interrupt response. Test this explicitly in Task 7 |
| Stateless resume across workers depends on correct LangGraph checkpoint semantics | Verify with integration tests that a different worker can claim a re-queued paused task and continue cleanly with `Command(resume=...)` |
| `human_response` column could leak PII | Keep it only until the resumed input has been durably consumed, then clear it in the same transaction as the next checkpoint or terminal-state write. Document this in Task 4 |

---

## A9. Orchestrator Guidance

- Use `docs/design-docs/phase-2/design.md` Sections 5, 7, 8 as the canonical design contract
- Task 1 must land first. Tasks 2 and 3 can proceed in parallel after Task 1
- The `LangfuseEndpointRepository`/`Service`/`Controller` pattern is the direct template for `TaskEventRepository`/`Service`
- Approval/reject/respond repository methods use the same CTE + `MutationResult` pattern as `cancelTask()` and `redriveTask()`
- Worker-side event recording uses direct asyncpg INSERT, but it must occur in the same transaction as the paired state mutation
- Reuse the existing `queued` + `new_task` claim path for HITL resume; do not add a separate worker-specific wake channel
- Do not implement budget-based pause logic — just add the `paused` status to the enum
- Do not implement non-idempotent tool guards — Track 5 handles that
- The `request_human_input` built-in tool is the MVP HITL entry point; approval gates for tool calls come in Track 5
- Default human-input-timeout is 24 hours (configurable per-agent in future tracks)

---

## A10. Key Design Decisions

1. **Pause states release the lease** — the graph checkpoints, transitions to a waiting state, clears lease ownership, and frees the worker to pick up other work while human action is pending.

2. **Resume is stateless** — approve/reject/respond persist the interrupt result, move the task back to `queued`, and any worker can claim it and resume from the checkpoint through the normal poller path.

3. **Worker-side events use direct asyncpg INSERT** — avoids coupling Python worker to Java API for audit writes. Both sides write to the same `task_events` table.

4. **`paused` status added now but not implemented** — prepares for Track 3 budget enforcement without extra migration later.

5. **`human_response` column on tasks** — stores the approval/rejection reason or freeform input so the resumed worker can read it. It is cleared only after the resume payload has been durably consumed.

6. **Event recording is durable, not best-effort** — `task_events` is the lifecycle audit trail, so paired task-state changes and event inserts must succeed or fail together.

---

## B. Agent Task Files

| Task | File | Description |
|------|------|-------------|
| Task 1 | [task-1-database-migration.md](agent_tasks/task-1-database-migration.md) | Schema: new statuses, task_events table, new task columns |
| Task 2 | [task-2-event-service.md](agent_tasks/task-2-event-service.md) | TaskEventRepository/Service, GET events endpoint |
| Task 3 | [task-3-hitl-api.md](agent_tasks/task-3-hitl-api.md) | Approve/reject/respond endpoints |
| Task 4 | [task-4-worker-interrupt.md](agent_tasks/task-4-worker-interrupt.md) | GraphInterrupt handling, reaper timeout |
| Task 5 | [task-5-event-integration.md](agent_tasks/task-5-event-integration.md) | Emit events from all state transitions |
| Task 6 | [task-6-console-updates.md](agent_tasks/task-6-console-updates.md) | Status badges, approval/input UI, events timeline |
| Task 7 | [task-7-integration-tests.md](agent_tasks/task-7-integration-tests.md) | E2E tests for approval, input, events flows |
