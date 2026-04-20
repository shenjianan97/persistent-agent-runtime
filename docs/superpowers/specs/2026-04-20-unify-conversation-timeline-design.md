# Unify Conversation + Timeline onto Checkpoints — Design

**Issue:** [#89](https://github.com/shenjianan97/persistent-agent-runtime/issues/89)
**Date:** 2026-04-20
**Scope:** New task under `docs/exec-plans/active/phase-2/track-7-follow-up/`
**Status:** Draft — for review before implementation planning

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

```
Worker writes:
  checkpoints   ← state["messages"] (LLM-facing turns)   [unchanged]
  task_events   ← markers + bodies in details JSONB      [extended]

API reads:
  GET /tasks/{id}/activity
    1. SELECT latest checkpoint row by task_id
    2. Deserialize state, extract messages
    3. SELECT task_events WHERE task_id = ?
    4. Merge by ordering key (checkpoint seq + event created_at)
    5. Return unified event stream

Console renders:
  Single "Activity" tab, role-anchored default view,
  "Show details" toggle exposes infra markers inline.
```

### Why on-demand projection, not materialization

**We are write-heavy.** Checkpoints fire on every graph step; Console reads are infrequent and polling-cached by the browser. Materializing a projection (trigger or worker dual-write) would amplify every hot-path write to benefit a cold-path read — the inverse of the standard "materialize when reads >> writes" heuristic.

**Reads are already cheap.** The read path is one primary-key lookup (`checkpoints` by `task_id`) plus one indexed scan (`task_events` by `task_id`). No joins, no aggregation. Deserialization happens in the API process, not the DB. Conversation length is bounded by compaction, so worst-case deserialization is tens of KB.

**No cache in v1.** Multiple API replicas make an in-process LRU fragmented, and Redis is infra we don't need yet. If production metrics show a hot endpoint, we add a targeted cache (local first) or narrow materialization *without* changing the API contract.

**Industry precedent:** Temporal computes Event Groups in the UI from raw Event History; LangSmith renders traces from the authoritative log; LangGraph Studio renders from `state["messages"]`. None materialize a full content projection.

### Why a single "Activity" tab (not two tabs kept)

Research on 2025-2026 agent-observability UIs (LangSmith, LangGraph Studio, Temporal, Smashing Magazine's design-patterns survey) shows the dominant pattern is **one view with mode toggles**, not separate tabs over separate stores. Users conceptually want to switch *level of detail*, not *which backend to read from*. The "developer mode" toggle is the canonical affordance for show/hide infra metadata.

Our current two-tab split is a legacy outlier. Since we have to touch the Console anyway to switch data source, unifying the view in the same change avoids perpetuating the mismatch.

### Risk mitigation

Landing backend + UI together is higher-risk than a backend-only cut. To de-risk:

1. Ship the new API endpoint behind a feature flag (`unified_activity_view`).
2. Ship the new Console tab behind the same flag.
3. When flipped on, old tabs remain as a fallback for one release.
4. Remove old code path and drop `task_conversation_log` in a follow-up commit/PR once the new path is stable.

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

**Checkpoint deserialization:** the payload format (msgpack/pickle blob vs JSONB column) is the single implementation unknown worth verifying during planning. If the payload is opaque in Java, options are (a) decode in API via a thin library, (b) have the worker project `messages` into a JSONB column on checkpoint write (no dual-write — same transaction), (c) rely on a Python sidecar. This choice belongs in the exec plan, not the spec.

### 2. Worker: extend `task_events` for markers currently in convlog

Add the following `event_type` values via a CHECK-constraint migration:

- `memory_flush`
- `system_note`
- `offload_emitted`
- `task_compaction_summary` (carries summary text body in `details.summary_text`; distinct from `task_compaction_fired` which carries only metadata)
- `hitl_pause_detail`, `hitl_resume_detail` (richer per-resume payload beyond existing lifecycle events)

Add `task_events` INSERTs for the new marker kinds in the same transaction as the checkpoint write. The `_convlog_append_*` helpers remain in Phase A for parity (dual-write during rollout); they are removed in Phase B once the flag has stabilized.

**No synthetic system messages.** Per research, treating infrastructure markers as messages in `state["messages"]` risks blurring the LLM-facing-context boundary that replace-and-rehydrate carefully maintains. All markers live in `task_events`.

### 3. Console: unified Activity tab

- New component `ActivityPane` under `services/console/src/features/task-detail/` replaces `ConversationPane` and existing Timeline pane.
- Default view: role-anchored chat-style turns (user / assistant / tool).
- Header toggle: "Show details" — reveals inline marker chips (compaction boundary with summary text, memory_flush, HITL pause/resume, offload rollup, lifecycle).
- Per-turn expander: click any row to see tokens, latency, cost, tool args (matches LangSmith per-trace detail panel).
- Scenario coverage: new Playwright scenario in `CONSOLE_BROWSER_TESTING.md` for the unified tab (smoke + details-toggle + per-row-expand). Update coverage matrix: sub-objects rendered here (compaction, memory, hitl) get parity assertions.

### 4. Deprecation path

- Phase A (same PR): backend endpoint + Console UI behind `unified_activity_view` flag. Worker still dual-writes.
- Phase B (follow-up, after flag validated): worker stops writing to `task_conversation_log`. Old tabs removed.
- Phase C (follow-up): drop `task_conversation_log` table via migration. Remove `ConversationLogRepository`.

Backfill is not needed: existing tasks' turn data already lives in `checkpoints`, and markers in `task_conversation_log` that aren't mirrored in `task_events` are acceptable to lose for historical tasks (user-facing parity starts from the flag flip).

## Tradeoffs

| Choice | Pro | Con |
|---|---|---|
| On-demand projection over materialization | Zero write amplification on hot path; matches Temporal/LangSmith/LangGraph convention | Read path does work per request (mitigated: compaction bounds size; PK lookup; no cache needed yet) |
| Single tab + toggle over two tabs | Matches industry pattern; unifies mental model with data model | Console redesign ships in same change as backend |
| Feature flag rollout | Safe revert path | Flag-cleanup follow-up needed |
| Markers in `task_events` only (no synthetic messages) | Single rule; preserves LLM-context boundary | One CHECK-constraint migration per new kind (cheap; one-liner) |
| Skip distributed cache in v1 | No new infra; aligned with early-stage platform | Revisit if metrics show hot-endpoint latency |

## Testing

- **Unit:** `ActivityProjectionService` merge/filter logic, including `include_details` filter and ordering-key edge cases (same timestamp between a checkpoint message and a task event).
- **Integration:** API endpoint against real `checkpoints` + `task_events` fixtures via `par-e2e-postgres` (port 55433).
- **Worker:** tests for each marker kind — `memory_flush`, `system_note`, `offload_emitted`, `task_compaction_summary`, `hitl_*_detail` — assert `task_events` row shape and that no `task_conversation_log` write happens when flag is on.
- **Console unit:** `ActivityPane` renders each event kind; toggle filters correctly.
- **Console browser (BLOCKING):** new Playwright scenario, smoke + details-toggle + per-row-expand. Flag on and off. Coverage matrix updated for `memory`, `context_management`, and `hitl` sub-objects.

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
