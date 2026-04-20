# Unify Conversation + Timeline onto Checkpoints — Design

**Issue:** [#89](https://github.com/shenjianan97/persistent-agent-runtime/issues/89)
**Date:** 2026-04-20
**Target landing:** multiple new tasks under `docs/exec-plans/active/phase-2/track-7-follow-up/` (decomposition in §Task decomposition below)
**Status:** Draft design spec — feeds `writing-plans` to produce per-task specs

## Problem

The task-detail page has two tabs — **Conversation** and **Execution Timeline** — backed by two different stores:

- **Conversation** reads `task_conversation_log`, a projection table the worker dual-writes via `_convlog_append_*` helpers in `services/worker-service/executor/graph.py`.
- **Timeline** reads `checkpoints` (our `PostgresDurableCheckpointer`-managed store, extending LangGraph's `BaseCheckpointSaver` with lease-aware columns) plus `task_events`.

The split was introduced by Track 7 Task 13 because Timeline originally rendered LangGraph checkpoint messages directly and showed `[tool output not retained…]` placeholders after in-place compaction. That rationale is no longer load-bearing:

- **Track 7 Follow-up Task 3** replaced in-place compaction with replace-and-rehydrate. `state["messages"]` is no longer mutated; only the LLM-facing projection is compacted. Checkpoints carry the raw journal.
- **Task 4 ingestion offload** rewrites large tool results to a placeholder *before* either store sees the message. Both tabs already show the same placeholder for big payloads.
- **We own the `checkpoints` schema.** It's not LangGraph-managed; it's our columns written by `PostgresDurableCheckpointer`.

Dual writes now cost us more than they save: two idempotency schemes, every new marker kind needs a CHECK-constraint migration on two tables, and bugs like the LangGraph `Command` unwrap issue (`03874a1`) affect only one of the two stores. This debt compounds with every Console or compaction change.

## Goal

Collapse onto a single source of truth:

- **`checkpoints`** holds `state["messages"]` (LLM-facing turns).
- **`task_events`** holds user-visible markers (lifecycle, compaction, HITL, memory, offload).
- The Console's task-detail page reads a single projection endpoint that merges the two by ordering key.
- `task_conversation_log` is deprecated and eventually dropped.

## Non-goals

- No changes to checkpoint retention or `PostgresDurableCheckpointer` schema.
- No changes to compaction semantics (replace-and-rehydrate stays as-is).
- No cross-task search or aggregation features.

## Architecture

### Data flow (after change)

**Writes:**
- Worker appends to `state["messages"]` as before, now stamping each new message with `additional_kwargs.emitted_at` (UTC ISO-8601). LangGraph checkpoint serialization preserves `additional_kwargs` through JSONB round-trip (already relied on for `recalled`, `content_offloaded` flags — see `services/worker-service/tests/test_shrink_summarized_recalls_to_pointers.py`).
- Worker writes marker rows into `task_events` (root namespace markers: compaction, memory, HITL, offload) with bodies in `details JSONB`.

**Reads (projection endpoint):**
- Fetch the latest **root-namespace** checkpoint (`checkpoint_ns = ''`) for the task, deserialize `checkpoint_payload.channel_values.messages`.
- Fetch `task_events` for the task ordered by `created_at`.
- Interleave the two streams on a shared time axis (turn timestamp = `message.additional_kwargs.emitted_at` with fallback to the containing checkpoint's `created_at`; marker timestamp = `task_events.created_at`).
- Return the merged stream as a discriminated-union `ActivityEvent` list.

**Console:**
- Single "Activity" tab, role-anchored default view, "Show details" toggle exposes infra markers inline.

### Ordering key

LangGraph `state["messages"]` is an ordered list but has no per-element timestamp by default. Replace-and-rehydrate (Track 7 Follow-up Task 3) guarantees the journal is preserved in the latest root checkpoint, but we still need to place `task_events` markers *between* the correct turns.

**Solution:** the worker stamps each message with `emitted_at` (UTC ISO-8601) in `additional_kwargs` at the moment of state append (inside the graph node, before the `return {"messages": [...]}` statement — not inside any convlog helper). The stamped message lands in `state["messages"]` and is persisted by the checkpointer; it is an intentional state mutation, not a projection-only annotation. This is compatible with replace-and-rehydrate: Track 7 Follow-up Task 3 forbids *destructive* mutation of the journal, not *annotation*, and Option-C pointer replacement already establishes that selected `additional_kwargs` writes through the journal are sanctioned.

**Projection usage:** the endpoint reads `message.additional_kwargs.emitted_at` as the turn's ordering key. Task events use their native `created_at`. The merge is a linear interleave on a shared time axis.

**Graceful fallback.** Deserialization treats missing `additional_kwargs.emitted_at` as a not-error condition; the ordering key falls back to the containing checkpoint's `created_at`. This is coarse (all messages in a checkpoint collapse to one timestamp) but monotonic and acceptable for:
- historical tasks started before the worker change,
- any message the worker may have appended through a path that doesn't stamp (defense-in-depth against incomplete rollout).

### Checkpoint namespace scope

The `checkpoints` table stores subgraph and tool checkpoints under non-empty `checkpoint_ns` values; only `checkpoint_ns = ''` is the root/main-graph state users see. The projection **must** scope to `checkpoint_ns = ''` in every read. Reuse the existing helper at `services/worker-service/core/checkpoint_repository.py:98` (`latest root-ns checkpoint id`) or its Java equivalent in `TaskRepository.java` (existing queries already filter on `checkpoint_ns = ''` — precedent set). The supporting index `idx_checkpoints_task_created ON (task_id, checkpoint_ns, created_at)` keeps this query on a primary-key-style path.

### Why on-demand projection, not materialization

**We are write-heavy.** Checkpoints fire on every graph step; Console reads are infrequent and polling-cached by the browser. Materializing a projection (trigger or worker dual-write) would amplify every hot-path write to benefit a cold-path read — the inverse of the standard "materialize when reads >> writes" heuristic.

**Reads are already cheap.** The read path is one primary-key lookup (`checkpoints` by `task_id`) plus one indexed scan (`task_events` by `task_id`). No joins, no aggregation. Deserialization happens in the API process, not the DB. Conversation length is bounded by compaction, so worst-case deserialization is tens of KB.

**No cache in v1.** Multiple API replicas make an in-process LRU fragmented, and Redis is infra we don't need yet. If production metrics show a hot endpoint, we add a targeted cache (local first) or narrow materialization *without* changing the API contract.

**Industry precedent:** Temporal computes Event Groups in the UI from raw Event History; LangSmith renders traces from the authoritative log; LangGraph Studio renders from `state["messages"]`. None materialize a full content projection.

### Why a single "Activity" tab (not two tabs kept)

Research on 2025-2026 agent-observability UIs (LangSmith, LangGraph Studio, Temporal, Smashing Magazine's design-patterns survey) shows the dominant pattern is **one view with mode toggles**, not separate tabs over separate stores. Users conceptually want to switch *level of detail*, not *which backend to read from*. The "developer mode" toggle is the canonical affordance for show/hide infra metadata.

Our current two-tab split is a legacy outlier. Since we have to touch the Console anyway to switch data source, unifying the view in the same change avoids perpetuating the mismatch.

### Feature flag mechanism

`unified_activity_view` is a **runtime configuration flag**, not a per-task setting:

- **Java API:** read via `@Value("${features.unifiedActivityView:false}")`, surfaced through the existing Spring configuration profile (follow whatever precedent exists for other `features.*` flags; if none, this establishes it).
- **Python worker:** read from the same config mechanism used by existing worker feature toggles (e.g., env-var-backed config). The worker needs the flag to decide whether to keep dual-writing to `task_conversation_log`.
- **Console:** reads the flag via the existing bootstrap/feature-config endpoint used by other server-driven UI gates.

It is **not** a `task_config` column; this is a platform-wide rollout switch, not a per-task knob. The exec plan should locate the nearest precedent flag and match its convention.

### Risk mitigation

Landing backend + UI together is higher-risk than a backend-only cut. To de-risk:

1. Ship the new API endpoint and Console tab behind the feature flag above.
2. When flipped on, old tabs remain as a fallback for one release.
3. Remove old code path and drop `task_conversation_log` in follow-up commits once the new path is stable.
4. **Parallel-safety.** The sub-tasks below span worker + API + Console + migration; they land sequentially (not as one mega-PR). The `task_events` schema migration + worker-side changes land first; the API endpoint and Console depend on those. Treat as sequential PRs per §Task decomposition. Where a single task touches both Python and Java (e.g. `ConversationLogRepository` deletion lands in Python worker and Java API together), the implementing agent is the sole editor for that file set — no parallel subagents on overlapping paths (per `CLAUDE.md §Parallel Subagent Safety`).

## Components

### 1. Backend: activity projection endpoint

**Contract:**

```
GET /tasks/{task_id}/activity?include_details={bool}
→ 200 { events: [ ActivityEvent ], next_cursor: string | null }

ActivityEvent (discriminated union on kind):
  - turn.user        { role, content, timestamp, checkpoint_seq }
  - turn.assistant   { role, content, tool_calls, timestamp, checkpoint_seq, cost_microdollars?, tokens? }
  - turn.tool        { role, tool_name, content, timestamp, checkpoint_seq }
  - marker.compaction_fired       { summary_text, turns_folded, timestamp }
  - marker.memory_flush           { details, timestamp }
  - marker.system_note            { body, timestamp }
  - marker.offload_emitted        { rollup, timestamp }
  - marker.hitl_paused            { reason, timestamp }
  - marker.hitl_resumed           { payload, timestamp }
  - marker.lifecycle              { event_type, status_before, status_after, timestamp }
```

**Implementation:**

- `ConversationLogRepository` and `TaskController` read paths are rewritten to use `CheckpointRepository` + `TaskEventRepository`.
- `ActivityProjectionService` (new) orchestrates the merge: fetches latest checkpoint, deserializes `state["messages"]`, fetches task events, interleaves by ordering key, maps to `ActivityEvent` DTOs.
- `include_details=false` (default): filter out marker events except `compaction_fired` (summary is user-visible even in default mode).

**Checkpoint deserialization:** `checkpoints.checkpoint_payload` is a JSONB column (`infrastructure/database/migrations/0001_phase1_durable_execution.sql:80`), so the Java API can read `state["messages"]` directly via Jackson/PGJsonb. No sidecar, no schema change. The exec plan should confirm the nested-message shape (`type`, `content`, `additional_kwargs`, `tool_calls`) matches LangChain's serialized schema for mapping to the `ActivityEvent` DTO.

### 2. Worker: extend `task_events` for markers currently in convlog

Extend the existing `task_events.event_type` CHECK constraint to admit the marker kinds currently unique to `task_conversation_log`. The exec plan owns the final list and the new marker names; the design contract is:

- All `task_conversation_log.kind` values that represent user-visible events must have a corresponding `task_events.event_type` (e.g., `memory_flush`, `system_note`, `offload_emitted`, `task_compaction_summary` for the summary body, HITL detail kinds). Matching to existing lifecycle event types where the semantics already coincide (e.g., `task_paused` / `task_resumed`) is preferred over inventing new names.
- **One migration for the whole bundle.** Precedent: `0018_conversation_log_offload_emitted_kind.sql` adds a single kind in one file; `0019_task_compaction_fired_event_type.sql` drops-and-recreates the entire CHECK constraint with the new values in a single file. This work needs exactly one migration (next number, likely `0020_`), not one per new kind. Treat "more migrations later when we add more kinds" as the sustained per-kind cost, not per-this-project cost.
- Marker bodies live in `task_events.details JSONB`. No new columns.

**Per-message `emitted_at` stamping.** Every time the worker appends to `state["messages"]`, it sets `additional_kwargs.emitted_at = <UTC ISO-8601>` on the new message(s) *before* returning from the graph node. Sites that currently feed `_convlog_append_*` are the same sites that need the stamp (pre-LLM human/tool turn append, LLM response append). Because `emitted_at` lands in state, not just in the convlog helper, the stamp survives checkpoint serialization and is visible to the projection on every subsequent read. The exec plan names the exact append sites; the design contract is: every new message in `state["messages"]` carries `emitted_at`.

**No synthetic system messages.** Per research, treating infrastructure markers as messages in `state["messages"]` risks blurring the LLM-facing-context boundary that replace-and-rehydrate carefully maintains. All markers live in `task_events`. The one permitted kind of `additional_kwargs` write is the per-message `emitted_at` stamp above, plus the already-sanctioned Option-C pointer-rewrite from Track 7 Follow-up Task 3.

### 3. Console: unified Activity tab

- New component `ActivityPane` under `services/console/src/features/task-detail/` replaces `ConversationPane` and existing Timeline pane.
- Default view: role-anchored chat-style turns (user / assistant / tool).
- Header toggle: "Show details" — reveals inline marker chips (compaction boundary with summary text, memory_flush, HITL pause/resume, offload rollup, lifecycle).
- Per-turn expander: click any row to see tokens, latency, cost, tool args (matches LangSmith per-trace detail panel).
- Scenario coverage: new Playwright scenario in `CONSOLE_BROWSER_TESTING.md` for the unified tab (smoke + details-toggle + per-row-expand). Update coverage matrix: sub-objects rendered here (compaction, memory, hitl) get parity assertions.

### 4. Deprecation path

- **Phase A:** migration lands (`0020_*`). Worker begins stamping `emitted_at` and writing new marker kinds to `task_events`. `_convlog_append_*` helpers continue writing to `task_conversation_log` in parallel.
- **Phase B:** API endpoint + Console `ActivityPane` ship behind `unified_activity_view` flag. Old tabs remain visible.
- **Phase B.5 (before flag flip):** run one-time backfill job. Validate parity by sampling tasks in both views.
- **Phase C:** flip the flag on. Monitor. Old tabs remain as a revert path for one release cycle.
- **Phase D (cleanup):** worker stops writing to `task_conversation_log`; remove Python `ConversationLogAppender` and all `_convlog_append_*` helpers; remove Java `ConversationLogRepository`; remove Console `ConversationPane`; drop `task_conversation_log` table via migration; remove the `unified_activity_view` flag.

### Backfill — required for in-flight tasks

If `unified_activity_view` flips while tasks are still running, their pre-flip marker history (`memory_flush`, `system_note`, `offload_emitted`, HITL-detail) would silently drop from the Activity view because those markers only exist in `task_conversation_log` today.

- **Shape:** a one-shot Python CLI living under `services/worker-service/` (reuses the worker venv and existing `task_events` insert path). Runs in CI on deploy or manually via `services/worker-service/.venv/bin/python -m scripts.backfill_convlog_to_task_events`. Not a worker task or a SQL migration; it's a one-time operational command.
- **Scope:** reads `task_conversation_log` rows for tasks whose `status` is non-terminal (the exact non-terminal set is read by reference from whatever enum currently defines it — do not hard-code the list here; the exec plan reads the source of truth and cites it). Inserts equivalent rows into `task_events` with `details.backfilled_from_convlog = true` for auditability.
- **Idempotence key:** reuse Task 13's precedent pattern — deterministic hash `sha256(task_id || convlog_row_id || event_type)` stored on `details.backfill_key`. Simpler `(task_id, created_at, event_type)` collides on same-millisecond events and is rejected.
- **Completed tasks are not backfilled.** They keep working through the old `ConversationLogRepository` read path during Phases A–C (the old Console tabs remain available until Phase D); after Phase D the Activity view falls back to checkpoint-only rendering with a "detailed history unavailable" banner for any historical task.
- **Turn data does not need backfill.** `state["messages"]` in `checkpoints` already contains the full journal (Track 7 Follow-up Task 3 replace-and-rehydrate invariant). The `emitted_at` fallback path in the ordering-key section covers messages written before the worker change.

## Tradeoffs

| Choice | Pro | Con |
|---|---|---|
| On-demand projection over materialization | Zero write amplification on hot path; matches Temporal/LangSmith/LangGraph convention | Read path does work per request (mitigated: compaction bounds size; PK lookup; no cache needed yet) |
| Single tab + toggle over two tabs | Matches industry pattern; unifies mental model with data model | Console redesign ships in same change as backend |
| Feature flag rollout | Safe revert path | Flag-cleanup follow-up needed |
| Markers in `task_events` only (no synthetic messages) | Single rule; preserves LLM-context boundary | Ongoing per-kind cost: every *future* new marker kind still needs a CHECK-constraint migration (this project bundles the known kinds into one migration) |
| Skip distributed cache in v1 | No new infra; aligned with early-stage platform | Revisit if metrics show hot-endpoint latency |

## Testing

- **Unit:** `ActivityProjectionService` merge/filter logic, including `include_details` filter and ordering-key edge cases (same timestamp between a checkpoint message and a task event).
- **Integration:** API endpoint against real `checkpoints` + `task_events` fixtures via `par-e2e-postgres` (port 55433).
- **Worker:** tests for each marker kind — `memory_flush`, `system_note`, `offload_emitted`, `task_compaction_summary`, `hitl_*_detail` — assert `task_events` row shape and that no `task_conversation_log` write happens when flag is on.
- **Console unit:** `ActivityPane` renders each event kind; toggle filters correctly.
- **Console browser (BLOCKING):** new Playwright scenario, smoke + details-toggle + per-row-expand. Flag on and off. Coverage matrix updated for `memory`, `context_management`, and `hitl` sub-objects.
- **Backfill job:** unit tests asserting idempotence on `(task_id, created_at, event_type)`, correct mapping of convlog kinds → `task_events.event_type`, and that terminal-status tasks are skipped.
- **Ordering-key invariants:** test that `emitted_at`-stamped messages interleave correctly with `task_events` markers around compaction boundaries, and that fallback-to-checkpoint-`created_at` still produces a monotonic stream for historical tasks without `emitted_at`.

## Task decomposition

This design spec feeds into multiple task specs under `docs/exec-plans/active/phase-2/track-7-follow-up/agent_tasks/`. `writing-plans` owns the authoring of each; the anticipated split (numbering continues from the current track-7-follow-up task count):

- **Task A — Worker foundations.** Migration `0020_*` adding marker event types. Worker emits to `task_events` for new marker kinds (dual-write alongside existing `_convlog_append_*`). `emitted_at` stamping at all journal-append sites. Python worker-only scope.
- **Task B — API projection endpoint.** `ActivityProjectionService` + `GET /tasks/{task_id}/activity` + deserialization of `state["messages"]` from JSONB + `ActivityEvent` DTOs + Java unit and integration tests. Depends on Task A. Java-only scope.
- **Task C — Console Activity pane.** `ActivityPane` component, feature-flag integration, unit tests, Playwright scenario per `CONSOLE_TASK_CHECKLIST.md`, coverage-matrix updates for `memory` / `context_management` / `hitl` sub-objects. Depends on Task B. Console-only scope.
- **Task D — Backfill.** One-shot Python CLI + idempotence tests. Runs operationally in Phase B.5 between Task C's merge and the flag flip. Depends on Task A (needs `task_events` kinds).
- **Task E — Deprecation (follow-up, Phase D).** Cleanup PR that removes Python `ConversationLogAppender`, `_convlog_append_*` helpers, Java `ConversationLogRepository`, Console `ConversationPane`, drops `task_conversation_log` via a new migration, removes the `unified_activity_view` flag. Ships after a bake period.

Sequential landing. No parallel work on overlapping files. Each task's spec (authored by `writing-plans`) will include the standard task-spec sections (Agent Instructions, CRITICAL PRE-WORK, Impacted Components, Acceptance Criteria with the embedded Console Task Checklist where relevant, CRITICAL POST-WORK, Dependency Graph entry), update `plan.md §A2/§A3`, and add a `progress.md` row.

## References

- Original design: `docs/exec-plans/completed/phase-2/track-7/agent_tasks/task-13-user-facing-conversation-log.md`
- Schema: `infrastructure/database/migrations/0006_runtime_state_model.sql` (`task_events`), `0017_task_conversation_log.sql` (deprecated), `0019_task_compaction_fired_event_type.sql` (precedent for adding event types)
- Checkpointer: `services/worker-service/checkpointer/postgres.py:150`
- Worker helpers to remove: `services/worker-service/executor/graph.py` (`_convlog_append_pre_llm_turns`, `_convlog_append_llm_response`, `_convlog_append_compaction_events`, `_convlog_append_offload_emitted`)
- API read path to rewrite: `services/api-service/src/main/java/com/persistentagent/api/repository/ConversationLogRepository.java`, `TaskController.java`
- Console to replace: `services/console/src/features/task-detail/ConversationPane.tsx`

Industry precedent:
- Temporal Event History + Event Groups (UI-computed, not materialized)
- LangSmith trace view (single hierarchical view + detail panel)
- LangGraph Studio (renders from `state["messages"]`)
- CHECK-constraint + TEXT + JSONB is the canonical evolution-friendly event-store shape (Crunchy Data, Close.com engineering, OneUptime, Azure Cosmos DB patterns)
