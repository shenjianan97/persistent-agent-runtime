<!-- AGENT_TASK_START: task-3-worker-service-core.md -->

# Task 3: Worker Service Core

## Agent Instructions
You are a software engineer implementing one module of a larger system.
Your scope is strictly limited to this task. Do not modify components outside
the "Affected Component" listed below.

**CRITICAL PRE-WORK:** Before beginning implementation, you MUST read the following context files to understand the system architecture and constraints:
1. `docs/PROJECT.md` 
2. `docs/design/phase-1/PHASE1_DURABLE_EXECUTION.md`

**CRITICAL POST-WORK:** After completing this task, you MUST update the status of this task to "Done" in the `docs/implementation_plan/phase-1/progress.md` file.

## Context
The Worker Service acts as the scalable, active host environment executing AI workflows efficiently. It independently retrieves pending workflows via polling constraints, guarantees unique process lock ownership using an automated heartbeat, and proactively cycles hung processes using an overlapping distributed reaper algorithm. 

## Task-Specific Shared Contract
- Treat `docs/design/phase-1/PHASE1_DURABLE_EXECUTION.md` as the canonical worker lifecycle contract. Do not invent alternate claim, heartbeat, retry, or reaper semantics.
- Claim logic must use the documented `FOR UPDATE SKIP LOCKED` pattern and respect `retry_after`.
- Heartbeat logic extends leases based on `task_id`, `tenant_id`, `status='running'`, and `lease_owner`; it must not depend on `tasks.version`.
- Reaper logic must handle both expired leases and total task timeout. Lease expiry requeues or dead-letters based on `retry_count` versus `max_retries`; timeout transitions directly to `dead_letter`.
- This task should provide reusable core primitives for Task 6. Do not embed LangGraph-specific execution logic here.

## Affected Component
- **Service/Module:** Worker Service Core (Python/Asyncio)
- **File paths (if known):** `services/worker-service/core/`
- **Change type:** new code

## Dependencies
- **Must complete first:** Task 1 (Database Schema)
- **Provides output to:** Task 6 (Graph Executor)
- **Shared interfaces/contracts:** Database schema `tasks` fields (`lease_owner`, `retry_count`, `retry_after`).

## Implementation Specification
Step 1: Implement the `Task Poller` as an asyncio loop that claims tasks using the `FOR UPDATE SKIP LOCKED` query from the design doc. Use `LISTEN new_task` as the primary wake mechanism and fall back to periodic polling if the connection drops. On empty polls, apply exponential backoff: `100ms → 200ms → 400ms → ... → 5s cap`. Bound concurrency with `asyncio.Semaphore(MAX_CONCURRENT_TASKS)` where `MAX_CONCURRENT_TASKS` defaults to 10. The poller must respect `retry_after` — do not claim tasks where `retry_after > NOW()`.
Step 2: Implement the `Heartbeat Task` running every 15s per active task. Each heartbeat extends `lease_expiry` by 60s using an UPDATE query that checks `task_id`, `tenant_id`, `status='running'`, and `lease_owner = worker_id` (not `version`). If the UPDATE returns 0 rows, the lease was revoked — signal the corresponding graph executor to stop immediately.
Step 3: Implement the distributed `Reaper Task` running on every worker instance at a jittered interval (30s +/- 10s). The reaper scans for two conditions: (a) expired leases (`lease_expiry < NOW()`) — requeue with incremented `retry_count` and exponential backoff if `retry_count < max_retries`, otherwise dead-letter with `retries_exhausted`; (b) task timeouts (`created_at + task_timeout_seconds < NOW()`) — transition directly to `dead_letter` with reason `task_timeout`. Both requeue paths must emit `pg_notify('new_task', worker_pool_id)` in the same transaction.
Step 4: Export structured logging with mandatory labels `task_id`, `worker_id`, and `node_name`. Log key events: `TASK_CLAIMED`, `LEASE_REVOKED`, `TASK_DEAD_LETTERED`. Emit metrics: `tasks.active`, `workers.active_tasks`, `queue.depth`, `poll.empty`, `leases.expired`.

## Acceptance Criteria
The implementation is complete when:
- [ ] Multiple worker instances can run concurrently, each claiming distinct tasks without duplication (verified via multi-worker integration test).
- [ ] Expired leases are requeued or dead-lettered by the reaper within one reaper interval.
- [ ] Timed-out tasks are transitioned to `dead_letter` with reason `task_timeout`.
- [ ] Heartbeat loss (simulated by stopping the heartbeat) results in the task becoming reclaimable after lease expiry.

## Testing Requirements
- **Unit tests:** Test backoff schedule progression, semaphore bounding, heartbeat interval timing, and reaper jitter range using asyncio test fixtures.
- **Integration tests:** Run multiple worker instances against a shared PostgreSQL test container. Verify: (a) no task is claimed by two workers simultaneously, (b) LISTEN/NOTIFY wakes a worker within 1s of task submission, (c) reaper reclaims tasks from a crashed worker.
- **Failure scenarios:** Simulate worker crash (stop heartbeat, verify reaper reclaims), DB connection drop (verify fallback to polling), and concurrent claim races.

## Constraints and Guardrails
- Use the exact claim query from the design doc's key queries section — do not invent a different claim CTE.
- Keep worker-core boundaries clean: polling, lease management, and reaper behavior belong here; graph assembly, provider calls, and tool dispatch belong to Task 6.

## Assumptions / Open Questions for This Task
- None

<!-- AGENT_TASK_END: task-3-worker-service-core.md -->
