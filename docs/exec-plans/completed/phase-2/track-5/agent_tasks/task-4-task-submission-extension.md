<!-- AGENT_TASK_START: task-4-task-submission-extension.md -->

# Task 4 — Task Submission: `attached_memory_ids` + `skip_memory_write`

## Agent Instructions

You are a software engineer implementing one module of a larger system. Your scope is strictly limited to this task.

**CRITICAL PRE-WORK:** Before beginning implementation, you MUST read:
1. `docs/design-docs/phase-2/track-5-memory.md` — sections "New table: `task_attached_memories`", "API Surface" (bottom half covering `POST /v1/tasks` extensions), and "Validation and Consistency Rules".
2. `services/api-service/.../controller/TaskController.java` and `service/TaskService.java` — current `POST /v1/tasks` path and task-detail serialiser.
3. `services/api-service/.../model/request/TaskSubmissionRequest.java` — the current request record that gains the two new fields.
4. `services/api-service/.../service/TaskEventService.java` — how `task_submitted` events are written today; the memory id list needs to show up in `details` JSONB.
5. The migration produced by Task 1 — `task_attached_memories` columns and FKs.
6. `services/api-service/.../service/TaskService.java` — snapshot-of-agent-config path; `skip_memory_write` is a per-task override that must survive onto the task row for the worker to read.

**CRITICAL POST-WORK:** After completing this task:
1. Run `make test` and `make e2e-test`. Fix any regressions.
2. Update the status in `docs/exec-plans/active/phase-2/track-5/progress.md` to "Done".

## Context

Customers attach past memory entries to a new task at submission time. The API must:

1. Validate each `memory_id` in a **single scoped SQL query** (`WHERE memory_id = ANY($1) AND tenant_id = :caller AND agent_id = :path_agent`).
2. Reject the whole submission with a uniform 4xx shape on any miss — no differentiation between "unknown id", "wrong tenant", or "wrong agent".
3. Persist the validated ids to `task_attached_memories` (one row per attached memory, `position` preserving order).
4. Mirror the id list into the `task_submitted` event's `details` JSONB.
5. Expose `attached_memory_ids` and `attached_memories_preview` on the task-detail response so the Console can render who-attached-what.

Separately, `skip_memory_write` is a per-task privacy override — when `true`, the worker treats the task as if `memory.enabled=false` for the task even if the agent has memory enabled (no `memory_note` / `memory_search` registered, no `memory_write` node, no dead-letter memory hook). The flag must flow from the submission payload into a persisted field the worker reads.

## Task-Specific Shared Contract

- **Request field names:** `attached_memory_ids: uuid[]` and `skip_memory_write: bool` on the JSON payload (Jackson snake-case per existing convention). Both optional.
- **Scope predicate:** single-query resolution using `WHERE memory_id = ANY($1) AND tenant_id = :caller AND agent_id = :path_agent`. Any count mismatch rejects the submission with a uniform 4xx.
- **Error shape on resolution miss:** identical across "unknown id", "wrong tenant", "wrong agent". No hint. No 403. The 404-not-403 disclosure rule in the design doc's validation section applies.
- **Persistence shape:** `task_attached_memories` rows are inserted **before** the task row is returned to the caller, in the same DB transaction as the task INSERT. `position` preserves the order of the input array (0-indexed).
- **Event mirror:** the `task_submitted` event's `details` JSONB gains a key `attached_memory_ids: [uuid, …]` mirroring the attached list **in the same `position` order as the join table rows**. Join table is authoritative on divergence; the event mirror exists so event consumers do not need to join. When the attached list is empty, the key is `[]` (not absent).
- **Immutability after creation:** follow-up (Track 4 `POST /v1/tasks/{task_id}/follow-up`) and redrive do NOT rewrite `task_attached_memories`. Those flows continue the conversation with already-seeded memories.
- **`skip_memory_write` storage:** a typed `tasks.skip_memory_write BOOLEAN NOT NULL DEFAULT FALSE` column. Task 1 provisions the column as part of `0011_agent_memory.sql` — this task reads and writes it from the service layer.
- **Default:** when `skip_memory_write` is absent on the payload, persist `false`.

## Affected Component

- **Service/Module:** API Service — Tasks
- **File paths:**
  - `services/api-service/.../model/request/TaskSubmissionRequest.java` (modify — two new fields)
  - `services/api-service/.../service/TaskService.java` (modify — validation + join-table insert + event detail + `skip_memory_write` storage)
  - `services/api-service/.../controller/TaskController.java` (modify — pass-through new fields if the controller does manual mapping)
  - `services/api-service/.../repository/TaskAttachedMemoryRepository.java` (new)
  - `services/api-service/.../service/TaskEventService.java` (modify — include `attached_memory_ids` in `task_submitted` event details)
  - `services/api-service/.../model/response/TaskDetailResponse.java` (or equivalent — add `attached_memory_ids` and `attached_memories_preview`)
- **Change type:** modification + new repository class

## Dependencies

- **Must complete first:** Task 1 (Migration + pgvector image pin) — `task_attached_memories` must exist, and `tasks.skip_memory_write` must be present as a typed column (Task 1 ships both).
- **Provides output to:** Tasks 6 (worker write path reads `skip_memory_write`), 7 (gates tool registration), 8 (attach injection), 9 (Console renders task detail), 10 (Console submit posts new payload).
- **Shared interfaces/contracts:** The `POST /v1/tasks` payload shape and the task-detail response shape.
- **Parallel-safety:** Task 3 also edits api-service Java files (no file-level overlap with this task's files, but both touch the same package tree). If dispatched concurrently, use `isolation: "worktree"` on one of the two per AGENTS.md §Parallel Subagent Safety.

## Implementation Specification

### Request validation

- `attached_memory_ids`: optional list of UUID strings. Reject if:
  - Any element is not a valid UUID syntax → 400.
  - Any element fails scope resolution → 4xx with the uniform shape. Do NOT report which id failed.
  - More than a cap of **50** (plan-added guard, NOT from the design doc; document the provenance in an inline code comment). The design doc specifies only the 10 KB token-footprint indicator on the Console side. The 50 cap is defence against blowing the initial prompt context regardless of indicator state.
- `skip_memory_write`: optional boolean. Reject only if the value is not boolean (Jackson handles this by default).
- Validation happens before the `tasks` row is INSERTed.

### Transaction shape

In a single DB transaction:

1. INSERT into `tasks` (with `skip_memory_write` populated or defaulted to `false`, and `agent_config_snapshot` captured as today).
2. INSERT into `task_attached_memories` — one row per `memory_id`, `position` = index in input array.
3. INSERT `task_submitted` event with `details` including `attached_memory_ids: [uuid, …]`.
4. COMMIT.

Any failure at step 2 or 3 rolls back the whole transaction — no partial task creation. Existing `TaskService` already wraps these steps in a transaction; extend it.

### Task detail response

Two new fields on the detail response:

- `attached_memory_ids: [uuid]` — resolved by querying `task_attached_memories WHERE task_id = :id ORDER BY position` (no join against memory). If empty, the field is `[]`, not absent.
- `attached_memories_preview: [{memory_id, title}]` — joined against `agent_memory_entries` on the current scope. Entries that no longer resolve (deleted memories) are **omitted** from this preview. The full `attached_memory_ids` list remains complete — the Console renders it as "1 attached memory no longer exists" when the preview is shorter than the id list.

### Response on submission

The existing `POST /v1/tasks` response shape gets the same two fields added (mirroring detail).

### Error envelope consistency

When `attached_memory_ids` resolution fails, return the same HTTP shape the existing API uses for invalid task-submission payloads (status + error code + message). The message should be generic — e.g., `"one or more attached_memory_ids could not be resolved"` — without naming the offending id or explaining the cause.

## Acceptance Criteria

- [ ] `POST /v1/tasks` accepts `attached_memory_ids: []` (empty array) and `attached_memory_ids` absent, both treated identically (no rows inserted into `task_attached_memories`).
- [ ] `POST /v1/tasks` with three valid memory ids persists three rows in `task_attached_memories` with `position = 0, 1, 2`, all in one transaction with the task row.
- [ ] `POST /v1/tasks` with one memory id belonging to the caller's tenant but a different agent rejects with a uniform 4xx.
- [ ] `POST /v1/tasks` with one memory id from a different tenant rejects with the same uniform 4xx.
- [ ] `POST /v1/tasks` with one syntactically-invalid UUID rejects with 400.
- [ ] `POST /v1/tasks` with more than 50 `attached_memory_ids` rejects with 400.
- [ ] On rejection, no `tasks` row, no `task_attached_memories` rows, no `task_submitted` event are created.
- [ ] `task_submitted` event's `details` JSONB includes `attached_memory_ids` mirroring the submitted list.
- [ ] `POST /v1/tasks` with `skip_memory_write = true` persists the flag on the task row.
- [ ] `skip_memory_write` defaults to `false` when absent.
- [ ] Task-detail response includes `attached_memory_ids` (always present, possibly `[]`) and `attached_memories_preview` (present, possibly `[]`).
- [ ] Deleting a memory entry (Task 3's DELETE endpoint) leaves `task_attached_memories` rows intact, and the preview silently drops the deleted entry while the full id list remains on the detail response.
- [ ] Follow-up and redrive on an existing task **do not** rewrite `task_attached_memories` — confirmed by an end-to-end test that follows up a task and asserts the attachment rows are unchanged.
- [ ] All new tests pass; existing task-submission tests pass unchanged.

## Testing Requirements

- **Service unit tests:** scope resolution miss paths (unknown id, wrong tenant, wrong agent) all return the uniform 4xx shape; transaction rollback on failure; join-table row count matches the input list length and `position` order.
- **Repository tests:** inserts, forward lookup (`WHERE task_id = :id ORDER BY position`), reverse lookup (`WHERE memory_id = :id`).
- **Integration tests:** full submission → DB assertions across `tasks`, `task_attached_memories`, `task_events`. Follow-up flow asserts no rewrite.
- **Regression:** existing task-submission tests pass. Detail response for a legacy task (no attachments, pre-Track-5) returns `attached_memory_ids: []` and `attached_memories_preview: []`.

## Constraints and Guardrails

- Do not broaden the error envelope to distinguish miss causes.
- Do not use any per-attached-memory metadata beyond `position` — attach time / reason / injection mode are explicitly deferred in the design doc.
- Do not load the resolved `title + summary + observations` into the task row in this task — the worker (Task 8) performs the initial-prompt injection by re-reading from `agent_memory_entries` at task start.
- Do not add a cascade rule from `agent_memory_entries` to `task_attached_memories` — the soft reference is intentional (audit preservation).
- Do not persist anything per-tenant about the `skip_memory_write` override; it is strictly a per-task flag.
- Do not add Console UI — Task 10 handles that.
- Do not expose scope-miss reasons in server logs above INFO. A log line that names the offending id is fine at DEBUG; production logs should not help an attacker probe.

## Assumptions

- Task 1 has shipped — `task_attached_memories` exists and `tasks.skip_memory_write BOOLEAN NOT NULL DEFAULT FALSE` has been added to the existing `tasks` table.
- The existing `TaskService.submit` transactional boundary can be extended — no refactor beyond adding the steps.
- The existing `TaskDetailResponse` is the single serialiser used by the task-detail GET endpoint (reused by Console).
- `position` is always equal to the input-array index; there is no de-duplication. Duplicate ids in the input array are rejected as part of UUID validation (document this and reject with 400).
- 50 is the attachment-count cap. The Console picker enforces the same cap from the UI side (Task 10).

<!-- AGENT_TASK_END: task-4-task-submission-extension.md -->
