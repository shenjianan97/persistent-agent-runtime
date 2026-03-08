<!-- AGENT_TASK_START: task-4-langgraph-checkpointer.md -->

# Task 4: LangGraph Postgres Checkpointer

## Agent Instructions
You are a software engineer implementing one module of a larger system.
Your scope is strictly limited to this task. Do not modify components outside
the "Affected Component" listed below.

**CRITICAL PRE-WORK:** Before beginning implementation, you MUST read the following context files to understand the system architecture and constraints:
1. `docs/PROJECT.md` 
2. `docs/design/PHASE1_DURABLE_EXECUTION.md`

**CRITICAL POST-WORK:** After completing this task, you MUST update the status of this task to "Done" in the `docs/implementation_plan/phase-1/progress.md` file.

## Context
Non-deterministic LLM iterations mandate checkpoint-resume protocols over event replay paradigms efficiently. The persistence adapter connects explicit LangGraph structures alongside our core state isolation constraints seamlessly blocking zombie writes leveraging worker identities. 

## Task-Specific Shared Contract
- Treat `docs/design/PHASE1_DURABLE_EXECUTION.md` as the canonical checkpointer behavior contract. The important safety rule is lease-aware checkpoint writes, not speculative library redesign.
- The checkpointer must validate active ownership using `task_id`, `tenant_id`, `status='running'`, and `lease_owner = worker_id` before persisting checkpoint state.
- `tasks.version` is not part of checkpoint write ownership checks.
- The implementation must follow the actual LangGraph/checkpoint package version pinned in the codebase. Do not guess or upgrade the contract inside this task.
- This task should export a stable constructor and exception behavior that Task 6 can consume directly.

## Affected Component
- **Service/Module:** LangGraph Checkpointer (Python)
- **File paths (if known):** `services/worker-service/checkpointer/`
- **Change type:** new code

## Dependencies
- **Must complete first:** Task 1 (Database Schema)
- **Provides output to:** Task 6
- **Shared interfaces/contracts:** Must implement the `BaseCheckpointSaver` contract for the LangGraph/checkpoint library version actually pinned in the codebase for Phase 1. Do not introduce an unverified version pin in this task.

## Implementation Specification
Step 1: Write `PostgresDurableCheckpointer` extending `BaseCheckpointSaver`. Map its initializer to receive active `worker_id` and structural `tenant_id` configurations automatically.
Step 2: Implement `put()` to persist checkpoint state to the `checkpoints` table. Before writing, execute a lease validation query that JOINs the `tasks` table to verify `status = 'running'` AND `lease_owner = self.worker_id` for the given `task_id` and `tenant_id`. Both the validation and the checkpoint INSERT must occur in the same database transaction.
Step 3: If the lease validation query returns no matching row (lease revoked, task cancelled, or reassigned), raise `LeaseRevokedException` instead of writing. This prevents split-brain corruption where a zombie worker persists stale state after its lease has been reclaimed.
Step 4: Reconcile `put_writes()`, `get_tuple()`, and any other required reader methods against the pinned library contract. Ensure serialization and deserialization match the actual dependency version used by the repo.

## Acceptance Criteria
The implementation is complete when:
- [ ] `put()` successfully persists checkpoint data when the worker holds an active lease.
- [ ] `put()` raises `LeaseRevokedException` when the lease has been revoked, cancelled, or reassigned — no checkpoint data is written.
- [ ] `get_tuple()` and `list()` correctly retrieve checkpoint history for a given thread (task).
- [ ] `put_writes()` persists pending writes to the `checkpoint_writes` table.

## Testing Requirements
- **Unit tests:** Mock the database connection to verify `put()` calls the lease validation query before INSERT, and raises `LeaseRevokedException` when validation fails.
- **Integration tests:** Against a PostgreSQL test container: (a) write checkpoints with a valid lease and verify retrieval via `get_tuple()`, (b) revoke the lease (update task status or lease_owner) and verify `put()` raises `LeaseRevokedException`, (c) verify `put_writes()` persists to `checkpoint_writes` with correct foreign key linkage.

## Constraints and Guardrails
- Never override BaseCheckpointSaver contracts dynamically altering structural inheritance semantics inherently.

## Assumptions / Open Questions for This Task
- RESOLVED: Use the versions pinned in Section 5.0 of the design doc: `langgraph==1.0.5`, `langgraph-checkpoint==4.0.0`, `langgraph-checkpoint-postgres==3.0.4`. Implement `BaseCheckpointSaver` from `langgraph-checkpoint` 4.0.0.

<!-- AGENT_TASK_END: task-4-langgraph-checkpointer.md -->
