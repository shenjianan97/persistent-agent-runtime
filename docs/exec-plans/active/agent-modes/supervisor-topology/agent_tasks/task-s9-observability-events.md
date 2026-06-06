<!-- AGENT_TASK_START: task-s9-observability-events.md -->

# Task S9 — Observability: Sub-Agent `task_events` Types + Migration `0025` + Activity Projection

## Agent Instructions

You are a software engineer implementing one module of a larger system. Your scope is strictly limited to this task: the **observability spine** for Supervisor fan-out — the migration that admits the new event types, the worker **emit contract** for them, and the API projection that turns them into a round → sub-agent tree the Console can render.

**CRITICAL PRE-WORK:** Before beginning implementation, you MUST read:
1. `docs/design-docs/agent-modes/design.md` — sections **"Observability: one task, sub-agent sub-steps"** (one task row; sub-agent activity as sub-steps carrying `iteration` round + `subtask` index), **"Partial subagent failure"** (why `subtask` must be a stable logical id distinct from `iteration` — a round-2 retry links back to its round-1 attempt), and **"Durability"** (resume-forward vs. redrive; these events are append-only over the parent's `task_events` timeline). Also the *Open decisions* bullet **"`iteration` / `subtask` marker shape"** — the *need* for both markers is decided; this task fixes their representation (in `details` JSONB on events, not new columns).
2. `docs/exec-plans/active/agent-modes/supervisor-topology/plan.md` — **§A4** (migration shape: additive CHECK extend, no new columns; `iteration`/`subtask` ride in `details`), **§A4.1 S9 row** (handoff contract), **§A6 deploy-order constraint** (migration MUST reach prod before emitting code), **§A7 Observability** (the exact event payloads this task owns), **§A0 invariants 1 & 6** (Pattern A: no sub-agent task rows; `subagent_results` keyed by `subtask`).
3. `infrastructure/database/migrations/0024_*.sql` — the **DROP + re-ADD** CHECK-constraint pattern (mirrors `0020`). Copy this shape exactly; the latest migration is `0024`, so this one is **`0025`**.
4. `infrastructure/database/migrations/0006_runtime_state_model.sql:25` — original `task_events.event_type` CHECK + the `task_events` table (`details JSONB` column already exists).
5. `services/worker-service/core/reaper.py:523` — `_insert_task_event(conn, task_id, tenant_id, agent_id, event_type, status_before, status_after, worker_id=, error_code=, error_message=, details=)`. This is the **only** emit helper; the new events go through it with `details` carrying `iteration`/`subtask`. **Atomicity caveat (corrected in PR review):** in the *reaper*, the caller owns one transaction pairing the event with the task-row update — that works because both writes are the caller's. The fan-out events **cannot** get that guarantee: LangGraph checkpoint persistence happens inside `PostgresDurableCheckpointer.aput`/`aput_writes`, which open **their own** connections + transactions (`services/worker-service/checkpointer/postgres.py:181-204`, `:247-260`) — an emit on a caller-owned connection can never share them. See the **at-least-once contract** below.
6. `services/api-service/.../service/ActivityProjectionService.java` — `mapMarker(TaskEventResponse)` (the `switch (type)` at ~line 476) and the `USER_VISIBLE_MARKERS` set (~line 56). This is where the new types become `marker.*` kinds.
7. `services/api-service/.../model/response/ActivityEventResponse.java` — the discriminated-union record (`kind` field names the payload; consumers ignore unrecognised fields — forward-compatible). New optional fields are added here.

**CRITICAL POST-WORK:** After completing this task:
1. Run `make e2e-test PYTEST_ARGS='-k subagent_event'` (the migration auto-applies on the isolated harness) and the API-service test for the projection (`make test` for Java, or the narrowest package that covers `ActivityProjectionService`). Fix any regression.
2. Confirm `.github/workflows/ci.yml` picks up `0025` via the migration glob (`[0-9][0-9][0-9][0-9]_*.sql`) — no manual CI wiring is needed for a migration, but verify the glob and state it in your PR description.
3. Create-or-update `docs/exec-plans/active/agent-modes/supervisor-topology/progress.md` — mark S9 status. If the file does not exist yet, create it with an S-stream task table and mark S9 done.

## Context

A Deep Research run is **one task row**. Sub-agent activity (each sub-agent's lifecycle, findings, and failures, grouped by iteration round) surfaces as **sub-steps on the parent's append-only `task_events` timeline**, never as separate task rows (design *Observability*; plan §A0 invariant 1 — Pattern A). For the Console to render a `round → sub-agent → steps` tree, three things must line up:

**SCOPE — marker skeleton only, NOT sub-agent transcripts (E5, plan §A11-E5 / §A1.9):** sub-agents run in **isolated context windows** that are *not* threaded into the parent task's `messages` channel. `ActivityProjectionService` projects the **`messages` channel** (`extractTurns` over checkpoint `messages`, ~`:100-118`), so the activity tree it can surface is exactly the **marker skeleton** these `task_events` carry — rounds (`supervisor_iteration`), sub-agent starts/findings/failures (`subagent_*`). It does **NOT** carry sub-agent turn-by-turn reasoning (tool calls, intermediate messages) — that lives in **Langfuse spans**. v1 ships the marker skeleton; persisting a *distilled sub-agent transcript* into a projected channel/marker is a **deferred option** (§A11-E5), out of scope here. Do not design the projection to imply a full sub-agent conversation view exists.

1. The DB must **admit** the new `event_type` values (the CHECK constraint currently rejects anything not in its allowlist — an `INSERT` of an un-admitted type raises a CHECK violation).
2. The worker must **emit** those events at fan-out points, carrying the `iteration` (round) and `subtask` (stable logical id) markers in the existing `details` JSONB.
3. The API must **project** them into tree-groupable `marker.*` kinds carrying `iteration`/`subtask` so the Console can group by round then sub-agent.

This task owns all three, **plus the emit contract** the fan-out call sites (S6/S7) consume. S9 does **not** build the Supervisor graph or the fan-out helper — it provides the helper function those tasks call and the payload schema they fill. The worker call sites live in S6 (fan-out / iteration) and S7 (findings); S9 lands the emit helper, its payload validation, and at least one emit at a call site that already exists or a thin shim S6/S7 wire into. Coordinate the exact wiring with S6/S7 via the handoff contract below.

**Deploy-order constraint (§A6 — load-bearing, state it in the PR):** the migration `0025` **MUST land in production before** any worker build that emits `subagent_*` / `supervisor_iteration` events. Otherwise the `INSERT INTO task_events` violates the CHECK and the emit fails (erroring the emitting node). **Merge S9's migration commit before S6/S7's emitting code ships to prod.** The migration is additive and non-breaking for existing rows, so it is safe to land early on its own.

**At-least-once event contract (load-bearing — corrected in PR review):** sub-agent events are **NOT atomic with checkpoint writes** and must not be specified as such. The checkpointer (`PostgresDurableCheckpointer.aput`/`aput_writes`) opens its own connections and transactions (`checkpointer/postgres.py:181-204`, `:247-260`); event emission happens during node execution on a separate caller-owned connection. Consequences the contract embraces instead of denying:
- On a crash, a marker can commit **without** its checkpointed state, or state can persist **without** its marker — transient skew is normal and self-heals on resume.
- **Per-turn resume re-emits:** a crashed-and-resumed inner step runs again and emits its events again — **duplicates are expected, not exceptional**.
- Therefore every `subagent_*` / `supervisor_iteration` event carries a **stable dedup key** — `(event_type, iteration, subtask)` (already in `details`) — and the **projection (and any consumer) must be duplicate-tolerant**: when building the round→sub-agent tree, dedup by that key (first-wins for `subagent_started`; last-wins for `subagent_failed` / result-bearing markers).

## Task-Specific Shared Contract

### New `task_events.event_type` values (admitted by `0025`)

| `event_type` | Emitted when | `details` payload (§A7) |
|---|---|---|
| `subagent_started` | a sub-agent subgraph is dispatched in a fan-out super-step | `{iteration, subtask, prompt_preview, tool_allowlist, depth}` |
| `subagent_finding` | a sub-agent emits a structured finding | `{iteration, subtask, finding_id, source_url}` (claim/quote go to the Langfuse span, **not** the row — bounds row size) |
| `subagent_failed` | a sub-agent exhausts its ceiling/timeout or errors | `{iteration, subtask, reason}` where `reason ∈ {ceiling, timeout, error}` |
| `supervisor_iteration` | the Supervisor closes a round and decides continue/stop | `{iteration, subtasks_emitted, decision, reason}` where `decision ∈ {continue, stop}` |

`iteration` is an **int** (round, 0-based or 1-based — pick one and document it; match whatever S6's reducer uses). `subtask` is a **string** stable logical id (so a round-2 retry links to its round-1 attempt — design *Partial subagent failure*). **Neither gets a column** — both ride in the existing `details` JSONB (plan §A4).

### Worker emit contract (consumed by S6/S7)

Provide a thin typed emit surface over `_insert_task_event` so S6/S7 call sites stay declarative and the payload schema is enforced in one place. Shape (names load-bearing for the handoff):

- `emit_subagent_started(conn, *, task_id, tenant_id, agent_id, iteration, subtask, prompt_preview, tool_allowlist, depth)`
- `emit_subagent_finding(conn, *, task_id, tenant_id, agent_id, iteration, subtask, finding_id, source_url)`
- `emit_subagent_failed(conn, *, task_id, tenant_id, agent_id, iteration, subtask, reason)`
- `emit_supervisor_iteration(conn, *, task_id, tenant_id, agent_id, iteration, subtasks_emitted, decision, reason)`

Each delegates to `_insert_task_event` with the correct `event_type` and a `details` dict built from its args. `status_before`/`status_after` are `None` for these (they are activity markers, not state transitions). `prompt_preview` MUST be truncated (e.g. ≤ 200 chars) so the row stays small. Helpers take a caller-owned connection and emit **at-least-once** — they are **NOT atomic with the checkpoint write** (the checkpointer owns its own connections/transactions; see the at-least-once contract above). Emissions must be safe to repeat: a resumed inner step re-emits, and the `(event_type, iteration, subtask)` dedup key is what consumers group/dedup on.

Place these alongside the existing emit helper (or a new `core/subagent_events.py` re-exported for the fan-out callers) — coordinate the import path with S3/S6 so they don't fork the machinery.

### API projection contract

- `ActivityProjectionService.mapMarker` gains cases:
  - `subagent_started` → `marker.subagent.started`
  - `subagent_finding` → `marker.subagent.finding`
  - `subagent_failed` → `marker.subagent.failed`
  - `supervisor_iteration` → `marker.supervisor.iteration`
- Each mapped marker carries `iteration` and `subtask` (where present) so the Console can group `round → sub-agent`. Surface them as **new optional fields** on `ActivityEventResponse` (`@JsonProperty("iteration") Integer iteration`, `@JsonProperty("subtask") String subtask`), `@JsonInclude(NON_NULL)` so existing kinds serialise unchanged. The raw `details` map already flows through; the typed fields are a convenience the Console reads directly (mirrors how `summaryText` is lifted out of `details` for `marker.compaction_fired`).
- Decide which of the four are **user-visible** (`USER_VISIBLE_MARKERS`, shown when `include_details=false`) vs. detail-only telemetry. `marker.subagent.finding` and `marker.supervisor.iteration` are user-meaningful (they show research progress); `marker.subagent.started` is closer to lifecycle telemetry (detail-only). `marker.subagent.failed` is user-meaningful (a customer should see a sub-agent failed even on the coarse view). Document the choice in a comment next to the set, matching the existing `marker.memory_written` precedent.
- **Forward-compat:** the `default -> null` branch in `mapMarker` already tolerates unknown types (a row written by a newer worker against an older API is dropped, not errored). Do not change that contract.

## Affected Component

- **Service/Module:** DB migration + Worker (emit helpers) + API (Activity projection)
- **File paths:**
  - `infrastructure/database/migrations/0025_agent_modes_subagent_events.sql` (new — DROP + re-ADD CHECK, additive)
  - `services/worker-service/core/subagent_events.py` (new — typed emit helpers over `_insert_task_event`) **or** add to the module that owns `_insert_task_event`; coordinate import path with S6/S7
  - `services/worker-service/tests/test_subagent_events.py` (new — emit + CHECK-acceptance + payload-shape tests)
  - `services/api-service/.../service/ActivityProjectionService.java` (modify — `mapMarker` cases + `USER_VISIBLE_MARKERS`)
  - `services/api-service/.../model/response/ActivityEventResponse.java` (modify — add optional `iteration` / `subtask` fields to the record + constructor call sites)
  - `services/api-service/.../service/ActivityProjectionServiceTest.java` (new or extend — fixture rows for each new type → grouped markers)
- **Change type:** new migration + new worker helper module + API projection extension

## Dependencies

- **Must complete first:** None structurally — the migration + emit helpers + projection can land independently of the graph build. In the **dependency graph (plan §A3)** S9 is parallel to the worker graph tasks; it must merge **before** S6/S7's emitting code reaches prod (§A6 deploy-order). The migration is the hard gate.
- **Provides output to:** S6 (emits `subagent_started` / `supervisor_iteration` at fan-out/iteration boundaries), S7 (emits `subagent_finding`; `subagent_failed` on ceiling/timeout/error), S10 (Console reads `marker.subagent.*` / `marker.supervisor.iteration` with `iteration`/`subtask` to render the tree), S11 (integration tests assert events insert + project correctly).
- **Shared interfaces/contracts:** the four `event_type` strings, the `details` payload schema per §A7, the four `emit_*` helper signatures, and the four `marker.*` kinds with `iteration`/`subtask` fields.

## Implementation Specification

### Migration `0025_agent_modes_subagent_events.sql`

- Header comment mirroring `0024`: state that this admits the four sub-agent fan-out markers, that the pattern is DROP + re-ADD on the single CHECK (per `0020`/`0024`), and that `iteration`/`subtask` ride in `details` JSONB (no new columns — Pattern A, plan §A4).
- **Lock-safety (E10, plan §A11-E10):** the naïve `DROP CONSTRAINT` + `ADD CONSTRAINT` re-validate pattern takes **`ACCESS EXCLUSIVE`** on `task_events` *and* re-validates **every existing row** against the new CHECK — a brief insert/read stall whose duration scales with the table size. **Prefer the two-step weaker-lock path:** `ADD CONSTRAINT task_events_event_type_check_v2 ... NOT VALID` (admits new inserts immediately, skips the full-table scan under the heavy lock) followed by a separate `VALIDATE CONSTRAINT task_events_event_type_check_v2` (takes only `SHARE UPDATE EXCLUSIVE`, does not block writes), then drop the old constraint. **OR**, if `task_events` is confirmed small in this deployment (state the row-count basis in the migration header + PR), the simple DROP+re-ADD is acceptable — pick one and document the reasoning. Either way the *end state* is the full allowlist + the four additions.
- `ALTER TABLE task_events DROP CONSTRAINT task_events_event_type_check;`
- `ALTER TABLE task_events ADD CONSTRAINT task_events_event_type_check CHECK (event_type IN ( ... ));` — the **full** current allowlist (all 21 values from `0024`) **plus** `subagent_started`, `subagent_finding`, `subagent_failed`, `supervisor_iteration`. Do not drop any existing value (additive, non-breaking). (If you take the `NOT VALID` → `VALIDATE` path above, express the same end-state allowlist through that two-step form instead of this single DROP+re-ADD.)
- **Do NOT add `subagent_heartbeat` (or any heartbeat value) to the CHECK allowlist.** The sub-agent heartbeat is a **Langfuse span event**, not a `task_events` row (§A11-E4); it must not become an admitted `event_type`. The four values above are the *complete* set this migration adds.
- Update the `COMMENT ON CONSTRAINT` to mention the agent-modes additions.
- No data backfill, no column add. Existing rows are unaffected.

### Worker emit helpers

Implement the four `emit_*` functions per the contract above. Each builds a `details` dict from its keyword args (omitting `None`s is fine), calls `_insert_task_event` with the right `event_type` and `status_before=None, status_after=None`, and truncates `prompt_preview`. Add a module-level constant for the preview cap. Do **not** open a transaction inside the helper — the caller owns the connection scope. Do **not** claim or rely on atomicity with the checkpoint write (impossible — `checkpointer/postgres.py:181-204`, `:247-260`); the at-least-once contract + dedup key is the guarantee.

### API projection

- Extend `ActivityEventResponse` with `Integer iteration` and `String subtask` (both `@JsonInclude(NON_NULL)`); update **all** constructor call sites in `ActivityProjectionService` (turns + existing markers pass `null` for the two new args — keep the positional argument order consistent).
- Add the four `mapMarker` cases mapping to the `marker.*` kinds; lift `iteration`/`subtask` out of `event.details()` into the typed fields (guard for missing keys / non-Number `iteration`, mirroring the `summaryText` extraction guard).
- Add the user-visible subset to `USER_VISIBLE_MARKERS` per the contract, with a comment.

## Acceptance Criteria

- [ ] `make e2e-test` applies `0025` and an `INSERT INTO task_events` with each of the four new `event_type` values succeeds (no CHECK violation); an insert of a bogus type still fails; an insert of `subagent_heartbeat` **also fails** (heartbeat is a Langfuse span event, deliberately NOT admitted — §A11-E4).
- [ ] The migration uses the weaker-lock `ADD ... NOT VALID` → `VALIDATE CONSTRAINT` form **or** documents (header + PR) that `task_events` is small enough for the DROP+re-ADD `ACCESS EXCLUSIVE` re-validation (E10).
- [ ] Each `emit_*` helper, called on a transaction-scoped connection, writes one row with the correct `event_type` and a `details` JSONB carrying the documented keys (incl. `iteration` int + `subtask` string where applicable); `prompt_preview` is truncated to the cap.
- [ ] `ActivityProjectionService` maps the four types to `marker.subagent.started` / `marker.subagent.finding` / `marker.subagent.failed` / `marker.supervisor.iteration`, each carrying `iteration` (and `subtask` where present) as typed fields. **Skeleton-only (E5):** the projection surfaces these markers from `task_events` and does **not** attempt to project sub-agent turn-by-turn `messages` (those isolated-context turns aren't in the parent's `messages` channel — they're in Langfuse). No criterion expects sub-agent transcript content in the projection.
- [ ] `include_details=false` returns the user-visible subset (`marker.subagent.finding`, `marker.subagent.failed`, `marker.supervisor.iteration`) and hides `marker.subagent.started`; `include_details=true` returns all four.
- [ ] Existing activity projection is **unaffected** — turns and pre-existing markers (`marker.compaction_fired`, `marker.hitl.*`, `marker.memory_written`, `marker.lifecycle`) serialise identically; the two new fields are absent (omitted) on those kinds.
- [ ] A row written with an `event_type` the API does not recognise is dropped (not errored) — the `default -> null` forward-compat branch is intact.
- [ ] **Duplicate tolerance (at-least-once contract):** two rows with the same `(event_type, iteration, subtask)` dedup key (the per-turn-resume re-emit case) project as **one** logical tree entry — first-wins for `marker.subagent.started`, last-wins for `marker.subagent.failed` / result-bearing markers. A test inserts a duplicate pair and asserts the grouped projection contains a single entry.
- [ ] `0025` is picked up by the CI migration glob (verified in `.github/workflows/ci.yml`); no manual CI wiring added.
- [ ] `docs/exec-plans/active/agent-modes/supervisor-topology/progress.md` reflects S9 done.

## Testing Requirements

- **Worker unit/integration tests** (`test_subagent_events.py`): one test per `emit_*` helper asserting the persisted `event_type` + `details` shape; one test asserting the migration admits all four types (insert-succeeds) and rejects a bogus one. Use the isolated harness DB — **never** raw `pytest tests/backend-integration` in a worktree; run via `make e2e-test PYTEST_ARGS='-k subagent_event'`. Any test that binds a port must use an ephemeral port (`scripts/e2e/free-port.py` / bind `:0`) — but these tests are DB-only and should not bind a TCP port at all.
- **API projection tests** (`ActivityProjectionServiceTest`): fixture `task_events` rows for each new type → assert mapped `kind`, `iteration`, `subtask`, and `include_details` filtering. Include a regression assertion that an existing marker projects unchanged.
- Mock nothing external — these tests are pure DB + pure projection.

## Constraints and Guardrails

- **Pattern A discipline (plan §A0 invariant 1):** do NOT add a `parent_task_id` / `sub_agent_id` column, a sub-agent task row, or any new column for `iteration`/`subtask`. They ride in `details` JSONB only.
- The migration is **additive**: never drop an existing allowlist value; re-ADD the full set.
- Do NOT put a finding's `claim` or `supporting_quote` in the `details` row (size bound — §A7; they live in the Langfuse span). Only `finding_id` + `source_url` go on the `subagent_finding` row.
- Do NOT change the `_insert_task_event` signature — wrap it, don't fork it. Do NOT promise atomicity with checkpoint writes anywhere (at-least-once + dedup is the contract).
- Do NOT build the Supervisor graph, the fan-out helper, or the actual call-site wiring decisions that belong to S6/S7 — S9 provides the emit helpers + payload schema + projection; S6/S7 call them.
- Do NOT touch the Console here (S10 reads the projected markers).
- Keep `mapMarker`'s `default -> null` forward-compat behavior — never throw on an unknown type.

## Assumptions

- The latest migration on `main` is `0024`; this one is `0025`. If another `0025_*` has landed, re-number to the next free slot and update the migration body's self-reference.
- `_insert_task_event` at `core/reaper.py:523` is the canonical event-emit helper and is import-stable; S6/S7 will import the new `emit_*` wrappers, not re-implement them.
- CI auto-applies migrations via the `[0-9][0-9][0-9][0-9]_*.sql` glob (plan §A6) — no service-container change is needed for a migration-only DB change.

<!-- AGENT_TASK_END: task-s9-observability-events.md -->
