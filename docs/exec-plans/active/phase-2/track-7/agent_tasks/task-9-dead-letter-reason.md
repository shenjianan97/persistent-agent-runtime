<!-- AGENT_TASK_START: task-9-dead-letter-reason.md -->

# Task 9 — Dead-Letter Reason: `context_exceeded_irrecoverable`

## Agent Instructions

**CRITICAL PRE-WORK:**
1. `docs/design-docs/phase-2/track-7-context-window-management.md` — section "Hard floor and dead-letter path".
2. `docs/design-docs/phase-2/design.md §5 Execution Audit History` — where `dead_letter_reason` values live.
3. `docs/exec-plans/completed/phase-2/track-2/` — how Track 2 added enum values (look for migrations and enum-update patterns).
4. `infrastructure/database/migrations/` — latest migration number to choose `0014_*`.
5. `services/api-service/src/main/java/com/persistentagent/api/enums/` (or wherever `DeadLetterReason` lives) — current enum shape.
6. `services/worker-service/core/worker.py` — `_handle_dead_letter` path; how existing reasons are plumbed.
7. `services/worker-service/executor/compaction/pipeline.py` (from Task 7) — `HardFloorEvent` definition and the caller site in `agent_node`.

**CRITICAL POST-WORK:**
1. Run `make test` AND `make e2e-test`. The enum addition affects API response serialisation; confirm existing Track 2 dead-letter tests still pass.
2. Update Task 9 status in `docs/exec-plans/active/phase-2/track-7/progress.md`.

## Context

When Tier 1 + 1.5 + 3 together cannot bring estimated input below the model's context window (and the protection-window shrink has also run), the task transitions to dead-letter with reason `context_exceeded_irrecoverable`. This is a safety-net event, expected to be rare in practice with the 25KB per-tool-result cap.

The reason must be plumbed through:

1. PostgreSQL enum (or enforced values set) for `dead_letter_reason` — additive migration.
2. Java `DeadLetterReason` enum on the API side.
3. Python constant on the worker side.
4. Pipeline hard-floor path invokes the worker's existing dead-letter transition with the new reason.

## Task-Specific Shared Contract

- New reason value (string, snake_case): `context_exceeded_irrecoverable`.
- Migration file: `infrastructure/database/migrations/0014_context_exceeded_dead_letter_reason.sql` (use the next available four-digit prefix — verify no `0014_*.sql` already exists; if it does, bump).
- The migration is additive and non-breaking: adds the value to the enum (`ALTER TYPE ... ADD VALUE IF NOT EXISTS`) or extends the CHECK constraint — match whatever approach Track 2 took.
- Java API surface: `DeadLetterReason.CONTEXT_EXCEEDED_IRRECOVERABLE` with JSON serialisation to `"context_exceeded_irrecoverable"`.
- Worker constant: Python string `DeadLetterReason.CONTEXT_EXCEEDED_IRRECOVERABLE = "context_exceeded_irrecoverable"` (or the existing constant module pattern).
- The hard-floor transition uses the worker's existing `_handle_dead_letter` path with `reason=DeadLetterReason.CONTEXT_EXCEEDED_IRRECOVERABLE`. No new transition code.

## Affected Component

- **Service/Module:** Database schema + API + Worker
- **File paths:**
  - `infrastructure/database/migrations/0014_context_exceeded_dead_letter_reason.sql` (new)
  - `services/api-service/src/main/java/com/persistentagent/api/enums/DeadLetterReason.java` (modify — or wherever the Java enum lives)
  - `services/api-service/src/test/java/.../DeadLetterReasonSerializationTest.java` (new or extend)
  - `services/worker-service/core/worker.py` or `core/constants.py` (modify — add Python constant)
  - `services/worker-service/executor/graph.py` (modify — `agent_node` handles `HardFloorEvent` from pipeline and invokes `_handle_dead_letter`)
  - `services/worker-service/tests/test_compaction_hard_floor.py` (new)
  - `tests/backend-integration/test_dead_letter_reasons.py` (modify if a reason-exhaustiveness test exists)
- **Change type:** migration + enum additions + hard-floor integration

## Dependencies

- **Must complete first:** Task 7 (pipeline emits `HardFloorEvent`). Task 9 wires the event to the dead-letter transition.
- **Parallel-safe with:** Tasks 2–6 (different files). Task 10 (Console — different area entirely).
- **Provides output to:** Task 11 (E2E test verifies the dead-letter transition).

## Implementation Specification

### Migration `0014_context_exceeded_dead_letter_reason.sql`

Match Track 2's migration style. Example shape:

```sql
-- 0014: Add context_exceeded_irrecoverable to dead_letter_reason enum.
-- Track 7 — Context Window Management hard-floor safety net.

ALTER TYPE dead_letter_reason ADD VALUE IF NOT EXISTS 'context_exceeded_irrecoverable';
```

If Track 2 used a CHECK constraint on a text column instead of a Postgres enum, match that approach. Inspect the current state of the `tasks` or `task_events` column `dead_letter_reason`.

### Java enum

Add `CONTEXT_EXCEEDED_IRRECOVERABLE("context_exceeded_irrecoverable")` to the existing enum. Keep the `@JsonValue` / `@JsonCreator` pattern already in place.

### Python constant

Add the constant in the same module Track 2 used (likely `services/worker-service/core/constants.py` or inline `worker.py`). Follow the existing style (either a plain module-level `str` or a `StrEnum`).

### Hard-floor handling in `agent_node`

After `pass_result = await compact_for_llm(...)`:

```python
hard_floor_event = next((ev for ev in pass_result.events if isinstance(ev, HardFloorEvent)), None)
if hard_floor_event is not None:
    await self._handle_dead_letter(
        task_data=task_data,
        reason=DeadLetterReason.CONTEXT_EXCEEDED_IRRECOVERABLE,
        details={
            "est_input_tokens": hard_floor_event.est_input_tokens,
            "model_context_window": hard_floor_event.model_context_window,
            "floor_reason": hard_floor_event.floor_reason,
        },
        ...
    )
    raise _DeadLetterRaised  # existing marker exception, if any
```

The existing `_handle_dead_letter` path persists the row to `task_events`, transitions the task status, releases the lease, etc. No new transition code is needed.

## Acceptance Criteria

- [ ] Migration `0014_*.sql` applies cleanly on a fresh Postgres (verified via `make db-reset` or equivalent).
- [ ] Applying the migration on an existing dev DB with pre-existing task rows does not mutate any existing row's `dead_letter_reason`.
- [ ] `POST /v1/tasks` existing dead-letter paths (`budget_exceeded`, `tool_failure`, `cancelled_by_user`, etc. — whatever Track 2 defined) continue to work; `make e2e-test` existing dead-letter tests pass.
- [ ] A synthetic task that forces Tier 3 to fail AND the hard floor to hit transitions to `status='dead_letter'` with `dead_letter_reason='context_exceeded_irrecoverable'`.
- [ ] The corresponding `task_events` row shows `event_type='task_dead_lettered'` with `details.floor_reason` populated.
- [ ] `GET /v1/tasks/{id}` returns `dead_letter_reason='context_exceeded_irrecoverable'` in the response payload.
- [ ] Console renders the new reason cleanly (Task 10 covers the UI mapping; Task 9 just needs to confirm no serialization errors).
- [ ] `make test` and `make e2e-test` green.

## Testing Requirements

- **Migration test:** `make e2e-test` already includes a fresh-DB migration step — confirm the new migration is picked up.
- **API serialization test:** `DeadLetterReason` enum serializes to `"context_exceeded_irrecoverable"` — test under the Java unit suite.
- **Worker unit test (`test_compaction_hard_floor.py`):** mock the pipeline to emit a `HardFloorEvent`; verify `agent_node` calls `_handle_dead_letter` with the correct reason.
- **E2E test:** synthesize a task + agent where Tier 3 is forced to fail (summarizer mocked to raise, or `summarizer_model` pointed at a non-existent model) AND the model context window is small enough that Tier 1/1.5 cannot hold the line; assert dead-letter transition + correct reason + event row.

## Constraints and Guardrails

- Do not remove any existing `dead_letter_reason` value. Migration is additive.
- Do not introduce new dead-letter reasons beyond `context_exceeded_irrecoverable` in this task. Stay focused.
- Do not change `_handle_dead_letter` logic beyond passing the new reason through — reuse the existing implementation.
- Do not wire the hard-floor event transition inside `compact_for_llm` itself — keep that function pure. `agent_node` handles the transition.
- Do not raise a bare `Exception` from the hard-floor path — use whatever marker exception Track 2's dead-letter path already uses.

## Assumptions

- Track 2's dead-letter flow is live and uses either a Postgres enum or a CHECK-constrained text column. If it's neither (e.g., free-text), align the migration with that shape.
- The Java `DeadLetterReason` enum is the canonical Java-side representation; the worker uses string constants directly.
- `task_events.dead_letter_reason` is the source of truth for the audit timeline; `tasks.dead_letter_reason` is the summary field.
- `make db-reset` is the standard way to verify migrations in local dev.

<!-- AGENT_TASK_END: task-9-dead-letter-reason.md -->
