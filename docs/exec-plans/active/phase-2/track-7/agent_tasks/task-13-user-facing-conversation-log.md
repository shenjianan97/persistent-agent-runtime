<!-- AGENT_TASK_START: task-13-user-facing-conversation-log.md -->

# Task 13 — User-Facing Conversation Log (separate from LangGraph checkpointer)

## Agent Instructions

Task 13 is **parallelizable across three independent subagents**. The shared contract (§Task-Specific Shared Contract — schema, `content` schema per `kind`, API response shape, ownership split) is fully specified so slices don't need to coordinate mid-flight.

- **Slice A — DB + Worker**: migration `0017`, `ConversationLogRepository` (Python, write-only), dual-write in `agent_node` including idempotency key + branch_id handling, worker unit + DB tests.
- **Slice B — API**: Java `ConversationLogRepository` (read-only), `ConversationLogService`, new endpoint on `TaskController`, Java unit + serialization tests, backend-integration E2E (runs against Slice A's migration — Slice A merges first OR Slice B stubs the repo behind a feature flag until A lands).
- **Slice C — Console**: `ConversationPane.tsx` + types + `client.ts` helper + unit tests against a mocked fixture of the §API endpoint response shape. `TaskDetailPage.tsx` integration. Playwright Scenario 17 added to `CONSOLE_BROWSER_TESTING.md`.

**Ownership rules (non-negotiable — these keep slices independent):**
- The Python repository is **write-only**. It has NO `list_entries` method. Slice A MUST NOT add one.
- The Java repository is **read-only**. It reads Postgres directly. Slice B MUST NOT call into Slice A's Python code.
- The `content` shape per `kind` and the API response JSON are authoritative in §Content schema + §API endpoint. Neither slice may deviate.
- Idempotency key format is authoritative: `sha256(task_id || branch_id || checkpoint_id || origin_ref)`. Origin ref rules are specified in §Worker write path.

**Merge order:** Slice A merges first (defines the schema); Slices B and C can merge in either order after that. The docs update (`track-7-context-window-management.md` §Customer-visible behavior changes, `progress.md` Task 13 row) is owned by whichever slice lands last.

Use `isolation: "worktree"` for every parallel slice.

**CRITICAL PRE-WORK:** Before beginning implementation, you MUST read:
1. `docs/design-docs/phase-2/track-7-context-window-management.md` — §"Customer-visible behavior changes" (lines 513–522). Task 13 inverts that section: customers should NOT see compaction placeholders by default.
2. `docs/exec-plans/active/phase-2/track-7/agent_tasks/task-8-pipeline-and-graph-integration.md` — Task 8 defined `RuntimeState` and `agent_node` integration; Task 13 hooks into the same call site with a parallel write to a new store.
3. `services/worker-service/executor/graph.py` — `agent_node`, `_build_graph`, the LangGraph checkpointer wiring. Locate the pre-LLM-call point where the new user/tool turn is appended to `state["messages"]`, and the post-call point where the AIMessage is appended.
4. `services/worker-service/executor/compaction/pipeline.py` — `compact_for_llm`. The compacted view is what the model sees; Task 13's log captures the view BEFORE compaction fires.
5. `infrastructure/database/migrations/0006_runtime_state_model.sql` — the `task_events` table pattern; Task 13 adds a *separate* `task_conversation_log` table (do NOT reuse `task_events`, which is enum-constrained for lifecycle events).
6. `services/api-service/src/main/java/com/persistentagent/api/controller/TaskController.java` — existing task-detail endpoints to mirror the new `GET /v1/tasks/{taskId}/conversation` after.
7. `services/console/src/features/task-detail/` — `CheckpointTimeline.tsx`, `TaskDetailPage.tsx`. The new "Conversation" pane lives alongside the existing timeline (it does NOT replace it).
8. Industry precedent:
   - **Claude Code** persists full conversation to `~/.claude/projects/<project>/*.jsonl`, appends a `compact_boundary` row on `/compact`; the terminal backlog still shows all prior messages. Source: decodeclaude.com/compaction-deep-dive.
   - **Cursor** (anti-pattern) replaces the chat panel on summarization, causing perceived context loss. Source: forum.cursor.com/t/summarizing-context-resets-context-in-chat/77724.
   - **Cline** shows a "summarization tool call" inline; raw history recoverable via checkpoint-restore.
   - **Aider** writes full transcript to a Markdown log on disk.
   - **Anthropic Cookbook** ("Context Engineering: memory, compaction, and tool clearing", March 2026) recommends storing the full `messages` list separately from what's sent to subsequent API calls.

**CRITICAL POST-WORK:**
1. Run `make worker-test`, `make api-test`, `make console-test`, `make e2e-test`. All suites must be green.
2. Orchestrator runs Playwright Scenario 17 (added here) to visually verify the Console pane renders correctly after a Tier 3 firing.
3. Update Task 13 status in `docs/exec-plans/active/phase-2/track-7/progress.md`.

## Context

Track 7 compaction is persisted in LangGraph checkpoints. Today the task-detail Console view renders messages from those checkpoints — so users see `[tool output not retained ...]` placeholders, `[... truncated N bytes ...]` markers, and truncated tool-call args. The design doc acknowledges this as a breaking change. Industry best practice (Claude Code, Aider, Anthropic Cookbook) keeps compaction invisible to the user for cheap/silent tiers and only surfaces an explicit boundary marker on retrospective summarization.

**The root cause of the leak** is that the Console's "execution history" is derived from the LangGraph checkpointer. Checkpoints are the model's working memory — they MUST be compacted for correctness and cost. They should not double as the user-facing audit trail.

**The fix** is to separate the two stores. The checkpointer continues to hold the model's view (compacted). A new append-only `task_conversation_log` table holds the user's view (raw). The worker dual-writes: one append to the log per new message, before `compact_for_llm` mutates `state["messages"]`. The Console's task-detail "Conversation" pane reads exclusively from the log.

**Design principle:**
- **Tiers 0, 1, 1.5 are invisible** to the user. The user sees the original tool output (subject only to a higher safety cap — see Constraints).
- **Tier 3 is visible** as a single inline divider: *"— Context summarized at this point —"* with an expand toggle showing the generated summary. Messages above and below the divider remain fully readable.
- **Pre-Tier-3 memory flush is visible** as an inline system event: *"— Memory flush fired —"*.

## Task-Specific Shared Contract

### Schema (migration `0017_task_conversation_log.sql`)

```sql
CREATE TABLE task_conversation_log (
    entry_id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id        TEXT NOT NULL,
    task_id          UUID NOT NULL REFERENCES tasks(task_id),
    -- Monotone ordering — NOT gapless. Consumers must page via `sequence > N`,
    -- never assume a contiguous 1..N range (gaps arise from redrive-branch
    -- soft-deletes and occasional insert retries). Postgres IDENTITY gives
    -- a single source of truth that survives rollback without holes in the
    -- write path.
    sequence         BIGINT GENERATED ALWAYS AS IDENTITY,
    -- Redrive attempt / branch this entry belongs to. When a task is redriven
    -- from an earlier checkpoint, a NEW branch_id is allocated and fresh
    -- entries get that branch_id. Pre-redrive entries stay in the table
    -- (soft-retained for audit) but are filtered out of the Console view by
    -- default. `task_branches.current_branch_id` or equivalent is the
    -- source of truth — see §Redrive below.
    branch_id        UUID NOT NULL,
    -- LangGraph checkpoint this entry was produced in (for cross-ref with
    -- the checkpointer and for dedup). NULL only for `system_note` entries
    -- that are not tied to a specific super-step.
    checkpoint_id    TEXT,
    -- Idempotency key — `sha256(task_id || branch_id || checkpoint_id ||
    -- origin_ref)` where origin_ref is the LangGraph message id for
    -- model/tool messages or a deterministic compaction-event id (e.g.,
    -- "tier3:<watermark_before>->{watermark_after}") for compaction entries.
    -- A duplicate insert with the same key is a no-op (ON CONFLICT DO NOTHING)
    -- — this makes every worker write idempotent across retries and crashes.
    idempotency_key  TEXT NOT NULL,
    kind             TEXT NOT NULL CHECK (kind IN (
        'user_turn',
        'agent_turn',
        'tool_call',
        'tool_result',
        'system_note',
        'compaction_boundary',
        'memory_flush'
    )),
    role             TEXT,                  -- 'user' | 'assistant' | 'tool' | 'system'
    content_version  SMALLINT NOT NULL DEFAULT 1,   -- bumped on schema change
    content          JSONB NOT NULL,                 -- shape per-kind (see §Content schema)
    content_size     INTEGER NOT NULL,               -- bytes of serialized content
    metadata         JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (task_id, idempotency_key)
);

CREATE INDEX idx_task_conversation_log_task_seq
    ON task_conversation_log (task_id, sequence);
CREATE INDEX idx_task_conversation_log_task_branch_seq
    ON task_conversation_log (task_id, branch_id, sequence);
CREATE INDEX idx_task_conversation_log_tenant
    ON task_conversation_log (tenant_id, created_at DESC);
```

`UNIQUE(task_id, idempotency_key)` replaces the earlier `UNIQUE(task_id, sequence)` — it's what dedups retries, and it lets `sequence` be a plain IDENTITY column (no `MAX+1` race, no advisory lock, no retry-on-23505 loop). Consumers paginate via `sequence > after_sequence` and tolerate gaps.

### Content schema per `kind` (v1)

Content is a JSONB blob whose shape is a function of `kind`. Every entry also has `content_version` (int, default 1). Implementers MUST NOT diverge from these shapes; new fields require bumping `content_version`.

| `kind` | `content` shape | `metadata` shape |
|--------|-----------------|------------------|
| `user_turn` | `{"text": str}` | `{}` |
| `agent_turn` | `{"text": str}` — text portion of the AIMessage | `{"message_id": str, "finish_reason": str \| null}` |
| `tool_call` | `{"tool_name": str, "args": object, "call_id": str}` — one entry per `AIMessage.tool_calls[*]` | `{"message_id": str}` |
| `tool_result` | `{"call_id": str, "tool_name": str, "text": str, "is_error": bool}` — `text` is Tier-0-capped (≤ 25KB) | `{"orig_bytes": int, "capped": bool}` |
| `system_note` | `{"text": str}` — platform-owned notes (rarely used in v1) | `{}` |
| `compaction_boundary` | `{"summary_text": str}` | `{"summarizer_model": str, "turns_summarized": int, "summary_bytes": int, "cost_microdollars": int, "tier3_firing_index": int}` |
| `memory_flush` | `{}` | `{}` |

Unknown-field behavior on the API read path: Java unmarshals via Jackson with `FAIL_ON_UNKNOWN_PROPERTIES=false` on `content`/`metadata` only, so a schema-v2 entry served to a schema-v1 Console degrades gracefully. Console renders any unrecognized `kind` as a neutral "system event" banner with raw JSON in a debug fold.

### Worker write path

Dual-write happens inside `agent_node` — NEVER inside `compact_for_llm` (pipeline stays pure). Write points:

1. **Pre-LLM turns** — after `agent_node` resolves the incoming `raw_messages` for the super-step, iterate `raw_messages[state.last_super_step_message_count:]` and append one entry per message. Each entry's `idempotency_key = sha256(task_id || branch_id || checkpoint_id || message_id)` where `message_id` is the LangChain message ID. The `ON CONFLICT DO NOTHING` clause turns redrive-re-processing into a no-op.
2. **LLM response** — append the response as `agent_turn` (text content only), plus one `tool_call` per `response.tool_calls[*]`. Same idempotency-key scheme.
3. **Compaction events** — iterate `PassResult.events` returned by `compact_for_llm`. For each `Tier3FiredEvent`: append `compaction_boundary`. For each `MemoryFlushFiredEvent`: append `memory_flush`. Idempotency key uses `f"tier3:{watermark_before}->{watermark_after}"` or `f"flush:{checkpoint_id}"` — stable across retries.

Write ordering guarantee: appends happen BEFORE the LangGraph super-step commits its checkpoint. If the checkpoint commit fails, the next super-step retry re-runs and the idempotency key prevents duplicate log entries.

Transaction boundaries: each log append is its own statement (no outer transaction around the super-step's log + checkpoint write). This is intentional — LangGraph's checkpointer owns its own transaction, and the log is best-effort audit. In the rare case where the log append succeeds but the super-step crashes before producing an AIMessage, the pre-LLM entries are correct (they describe what went in); the missing agent_turn/tool_call entries are re-emitted on retry with the same idempotency keys.

### API read path

**Java owns the read path.** The Python `ConversationLogRepository` is write-only. The API service has its own Java `ConversationLogRepository` reading directly from Postgres — no cross-service RPC.

`GET /v1/tasks/{taskId}/conversation?after_sequence={N}&limit={M}&include_archived_branches={bool}`:

- Default `include_archived_branches=false` — returns only entries from the task's current branch (what the user should see).
- When `true`, returns all branches; the Console's debug/operator mode may use this, but the default pane does not.
- Default `limit=200`; max `limit=1000`. (Lowered from the earlier draft after Codex review — large pages are unnecessary given 5s polling.)
- Pagination: `next_sequence` is the max `sequence` of the returned page when `len(entries) == limit`, else `null`.
- Tenant-scoped via existing `TaskController` tenant guard.
- 404 when task doesn't exist or belongs to another tenant.

### Console task-detail "Conversation" pane

- New tab alongside the existing `CheckpointTimeline` (Timeline is kept — serves operator/infra audience).
- `ConversationPane` polls `/v1/tasks/{id}/conversation?after_sequence={lastSeq}` every 5 s while task is non-terminal.
- Entry rendering by `kind` (see §Content schema for exact shapes).
- Compaction-boundary entries render as an expandable inline divider. Memory-flush entries render as a single-line banner.

## Affected Component

- **Service/Module:** Worker, API, Console, DB
- **File paths:**
  - `infrastructure/database/migrations/0017_task_conversation_log.sql` (new)
  - `services/worker-service/core/conversation_log_repository.py` (new)
  - `services/worker-service/executor/graph.py` (modify — dual-write inside `agent_node`)
  - `services/worker-service/tests/test_conversation_log_repository.py` (new, DB-touching)
  - `services/worker-service/tests/test_conversation_log_integration.py` (new — Tier-3 + flush boundary entries)
  - `services/api-service/src/main/java/com/persistentagent/api/controller/TaskController.java` (modify — new endpoint)
  - `services/api-service/src/main/java/com/persistentagent/api/service/ConversationLogService.java` (new)
  - `services/api-service/src/main/java/com/persistentagent/api/model/response/ConversationEntryResponse.java` (new)
  - `services/api-service/src/main/java/com/persistentagent/api/repository/ConversationLogRepository.java` (new)
  - `services/api-service/src/test/java/.../ConversationLogServiceTest.java` (new)
  - `tests/backend-integration/test_conversation_log_endpoint.py` (new — end-to-end POST task + GET conversation)
  - `services/console/src/api/client.ts` (modify — `listConversation(taskId, afterSequence?)`)
  - `services/console/src/features/task-detail/ConversationPane.tsx` (new)
  - `services/console/src/features/task-detail/TaskDetailPage.tsx` (modify — add Conversation tab)
  - `services/console/src/features/task-detail/__tests__/ConversationPane.test.tsx` (new)
  - `services/console/src/types/index.ts` (modify — `ConversationEntry` types)
  - `docs/CONSOLE_BROWSER_TESTING.md` (modify — add Scenario 17: Conversation pane + compaction boundary visual)
  - `docs/design-docs/phase-2/track-7-context-window-management.md` (modify — §"Customer-visible behavior changes" update: only Tier 3 and memory-flush are visible; Tiers 0/1/1.5 are invisible via the log)
  - `docs/exec-plans/active/phase-2/track-7/progress.md` (modify — add Task 13 row, mark Done)

- **Change type:** new migration + new repo / service / controller / UI + worker dual-write + doc update

## Dependencies

- **Must complete first:** Tasks 1–12 (all of Track 7 — pipeline, events, and dead-letter path must be live).
- **Parallel-safe with:** None in Track 7 (adds a new slice across all three services).
- **Provides output to:** Future Phase 3+ work on per-task audit export and customer-facing compaction transparency.

## Implementation Specification

### Migration `0017_task_conversation_log.sql`

As shown in §Task-Specific Shared Contract. Table is additive; no existing row changes. CHECK constraint on `kind` follows the Track 2 DROP+ADD pattern for future additions. `content_version` starts at 1; schema evolution bumps it.

### Redrive and `branch_id` semantics

LangGraph's `rollback_last_checkpoint` restores graph state from a prior checkpoint. Post-rollback, the next super-step re-runs with the restored state. Without `branch_id`, this would either (a) write duplicate entries (if idempotency key alone dedups by message id, the redriven super-step produces the *same* message ids and dedups correctly) or (b) produce divergent continuations that contradict earlier entries.

**Rule:** allocate a fresh `branch_id` (UUID) every time the worker's redrive path restores from an earlier checkpoint. Track the current branch on the task itself (new `tasks.current_branch_id UUID` column, or piggyback on the existing `task_events` stream — ADR pick one at implementation time; favor the column for query simplicity). The worker writes new log entries with the current `branch_id`. The API's read path filters `branch_id = tasks.current_branch_id` by default.

**Ordering under redrive:** `sequence` is a global IDENTITY, so it keeps increasing across branches. Within a given branch, `sequence` is monotone but NOT gapless (redriven-over entries stay in the table with older `branch_id`s; their sequences live in the earlier range). Pagination works uniformly: `sequence > after_sequence` always returns entries the client hasn't seen.

**Idempotency interaction with redrive:** the idempotency key includes `branch_id`, so the same message re-emitted under a new branch writes a new row. Redrive that does NOT advance the branch (no rollback, just retry) reuses the same `branch_id` and dedups cleanly via the idempotency key.

### Worker ownership & repository

**Python side — write only.** `services/worker-service/core/conversation_log_repository.py`:

```python
class ConversationLogRepository:
    async def append_entry(
        self,
        *,
        task_id: str,
        tenant_id: str,
        branch_id: str,
        checkpoint_id: str | None,
        idempotency_key: str,
        kind: Literal[
            "user_turn", "agent_turn", "tool_call", "tool_result",
            "system_note", "compaction_boundary", "memory_flush",
        ],
        role: str | None,
        content: dict,
        metadata: dict | None = None,
    ) -> int | None:
        """Insert one entry. Returns assigned `sequence`, or None if the
        idempotency key already existed (ON CONFLICT DO NOTHING swallowed it).
        """
```

Python MUST NOT expose a `list_entries` method; the API reads Postgres directly (see next).

**Java side — read only.** `services/api-service/src/main/java/com/persistentagent/api/repository/ConversationLogRepository.java` with a single method:

```java
List<ConversationEntryRow> findByTask(
    UUID taskId,
    UUID currentBranchId,
    long afterSequence,
    int limit,
    boolean includeArchivedBranches
);
```

No cross-service calls. The two repositories speak to the same table from opposite directions.

### API endpoint

`GET /v1/tasks/{taskId}/conversation?after_sequence={N}&limit={M}&include_archived_branches={bool}`:

- Response shape (Java record `ConversationEntryResponse`):
  ```json
  {
    "entries": [
      {
        "sequence": 101,
        "branch_id": "9a6b...-4a",
        "kind": "user_turn",
        "role": "user",
        "content_version": 1,
        "content": {"text": "Do the thing"},
        "metadata": {},
        "created_at": "2026-04-19T..."
      },
      {
        "sequence": 102,
        "branch_id": "9a6b...-4a",
        "kind": "tool_call",
        "role": "assistant",
        "content_version": 1,
        "content": {"tool_name": "sandbox_read_file", "args": {"path": "/..."}, "call_id": "call_abc"},
        "metadata": {"message_id": "ai_xyz"}
      },
      {
        "sequence": 140,
        "branch_id": "9a6b...-4a",
        "kind": "compaction_boundary",
        "role": "system",
        "content_version": 1,
        "content": {"summary_text": "Earlier: the agent explored /tmp and found 14 log files..."},
        "metadata": {"summarizer_model": "claude-haiku-4-5", "turns_summarized": 18, "summary_bytes": 412, "cost_microdollars": 238, "tier3_firing_index": 1}
      }
    ],
    "next_sequence": null
  }
  ```
- `next_sequence = max(sequence)` when `len(entries) == limit`, else `null`.
- Default `limit=200`; max `limit=1000`.
- `include_archived_branches` default `false` — shows only current-branch entries.
- Tenant guard reuses the existing `TaskController` pattern.
- 404 when task doesn't exist or belongs to another tenant.

### Console "Conversation" pane

- New tab on `TaskDetailPage` alongside the existing Timeline. Timeline is kept unchanged (operator/infra audience; Conversation pane is customer audience).
- `ConversationPane` polls `GET /v1/tasks/{id}/conversation?after_sequence={lastSeq}` every 5 s while task is non-terminal. Stops on terminal state.
- Entry rendering by `kind` (using the content schema in §Content schema):
  - `user_turn` — user bubble
  - `agent_turn` — assistant bubble
  - `tool_call` — collapsed card: tool name + args preview; expandable to full args
  - `tool_result` — collapsed card: head of `text` + expand; `metadata.capped=true` surfaces a "truncated at ingestion" tag
  - `compaction_boundary` — expandable inline divider: "— Context summarized at this point —"; expanded view shows `summary_text`, `summarizer_model`, `turns_summarized`, `summary_bytes`
  - `memory_flush` — single-line system banner
  - Unknown `kind` or future `content_version` — neutral "system event" banner with raw JSON in a debug fold

### Retention (v1 → Phase 3+ follow-up)

The log stores Tier-0-capped tool results (≤ 25KB each) in `jsonb`. A long-running task with hundreds of tool calls can produce multi-MB log footprints per task.

**v1 posture:** accept unbounded per-task growth. `make e2e-test` should include a 500-tool-call fixture that confirms Postgres handles the volume (hundreds of rows, tens of MB total, sub-100ms queries per page). No per-task row cap, no TTL.

**Phase 3+ follow-up (deferred, NOT in scope of Task 13):** for `tool_result` entries > N KB (N TBD based on telemetry), store a 2KB preview in `content.text` and a pointer into the existing `task_artifacts` table (`content.artifact_s3_key`). Console renders the preview + a "Load full result" button. This is a clean follow-on because the `content_version` field already exists — bump to 2 and ship.

### Docs update

`docs/design-docs/phase-2/track-7-context-window-management.md` §"Customer-visible behavior changes" — rewrite to reflect the new invariant:

> ### Customer-visible behavior changes (v2, per Task 13)
>
> Compaction is invisible to the customer by default. The Console's task-detail Conversation pane reads from a separate append-only `task_conversation_log` table, not from LangGraph checkpoints. Customers see:
>
> - **Tier 0 per-result cap (25KB head+tail):** applied at ingestion; the conversation log stores the same capped form the model sees. This is the only tier that surfaces in the user view — rarely triggered in practice (only on tools returning >25KB).
> - **Tier 1 (tool-result clearing):** INVISIBLE. The conversation log retains the full Tier-0-capped tool result; Tier 1 only affects what the model is shown.
> - **Tier 1.5 (tool-call arg truncation):** INVISIBLE.
> - **Tier 3 (retrospective summarization):** VISIBLE. A `compaction_boundary` entry is appended to the conversation log with the generated summary. The Console renders this as an expandable inline divider; messages above and below remain fully visible.
> - **Pre-Tier-3 memory flush:** VISIBLE. A single-line banner indicates the one-shot was inserted.
>
> The LangGraph checkpoint remains the source of truth for the model's view and for resume/redrive semantics. The conversation log is best-effort audit data; an append failure does not fail the task.

## Acceptance Criteria

- [ ] Migration `0017_task_conversation_log.sql` applies cleanly on a fresh DB and on an existing DB with pre-existing tasks (no row churn). `branch_id` column present; `sequence` is `GENERATED ALWAYS AS IDENTITY`; `UNIQUE(task_id, idempotency_key)` is the dedup constraint.
- [ ] Worker appends exactly one entry per new user/tool/agent message per super-step. Retrying the same super-step (same `branch_id`, same `checkpoint_id`, same message id) produces ZERO new rows (ON CONFLICT DO NOTHING path is exercised by the test).
- [ ] Redrive that allocates a new `branch_id` produces a fresh entry per re-emitted message (same message re-emitted under a new branch does NOT collide on the idempotency key).
- [ ] Tier 3 firing produces exactly one `compaction_boundary` entry per firing; `content.summary_text` equals the generated summary; `metadata.turns_summarized` matches the `Tier3FiredEvent` payload.
- [ ] Pre-Tier-3 memory flush produces exactly one `memory_flush` entry per task per branch.
- [ ] Tier 1 / Tier 1.5 firings do NOT produce any conversation-log entries.
- [ ] Per-tool-result cap still applies to conversation-log `tool_result` entries (≤ 25KB; `metadata.capped=true` when truncated at ingestion).
- [ ] `GET /v1/tasks/{taskId}/conversation` returns entries with `sequence > after_sequence`, filtered to the current branch when `include_archived_branches=false`. Pagination `next_sequence = max(sequence)` when page is full, else `null`. Tenant isolation enforced.
- [ ] `content` shapes per `kind` match §Content schema exactly (test asserts Jackson deserialises every `kind` into its documented shape).
- [ ] `content_version=1` for every v1 entry; a deliberately injected entry with `content_version=2` is still served by the API and rendered as a debug-fold by the Console.
- [ ] Console Conversation pane renders all seven `kind` values correctly; polling stops at terminal state.
- [ ] Console CheckpointTimeline pane still renders its existing checkpoint/HITL/redrive events — no regression.
- [ ] Task with a forced Tier 3 firing renders an expandable compaction-boundary divider; expanding shows the summary text.
- [ ] If the conversation-log append fails (simulated DB error on the log write path), the task still completes successfully; a structured warning is logged.
- [ ] If the LangGraph checkpoint commit fails AFTER log entries landed, the super-step retry re-emits the same message ids and the ON CONFLICT path prevents duplicates.
- [ ] `make worker-test`, `make api-test`, `make console-test`, `make e2e-test` all pass.
- [ ] Orchestrator Playwright Scenario 17 passes.

## Testing Requirements

- **Worker DB test** (`test_conversation_log_repository.py`): insert, sequence monotonicity under concurrency (10 parallel appends for the same `task_id` produce a contiguous `1..10` sequence), list with pagination, tenant scoping at the repo level.
- **Worker integration test** (`test_conversation_log_integration.py`): run a synthetic graph where Tier 3 fires; assert the log contains user + agent + tool entries plus a `compaction_boundary` entry with correct metadata; assert Tier 1 firing does NOT add any log entries.
- **API unit test**: endpoint shape, pagination, tenant isolation (404 when asking for a task owned by another tenant).
- **Backend-integration E2E** (`test_conversation_log_endpoint.py`): `POST /v1/tasks` → wait until complete → `GET /v1/tasks/{id}/conversation` → assert entries count and shape.
- **Console unit test** (`ConversationPane.test.tsx`): renders each `kind`; expand/collapse on `compaction_boundary`; polling stop on terminal status; error boundary on network failure.
- **Playwright Scenario 17**: Create an agent with a tiny `summarizer_model`; submit a long task; watch the Conversation pane populate in real time; trigger Tier 3 via a deliberately over-long tool output; assert the compaction-boundary divider appears with an expandable summary; assert tool results above the divider are still readable.

## Constraints and Guardrails

- Do NOT derive the Conversation pane from LangGraph checkpoints. The separation is the whole point.
- Do NOT remove or rename the existing `CheckpointTimeline` — it serves a different purpose (operator/infra view).
- Do NOT apply Tier 1 / 1.5 / 3 transforms to the conversation log. Tier 0 (per-result cap) still applies — the log is NOT a raw-binary blob store.
- Do NOT fail a task on conversation-log write failure — best-effort semantics. Log a structured warning and continue.
- Do NOT inline `summary_text` into the `messages` channel in state (that's already done by Task 8's summarizer). The conversation log's `compaction_boundary.content.summary_text` MAY duplicate the summary — intentional (log is self-contained for export).
- Do NOT expose the conversation-log write path to customer tools or MCP servers. Platform-owned.
- Do NOT batch conversation-log writes across super-steps. Append per-message, synchronously, within the super-step boundary.
- Do NOT add conversation-log content to Langfuse traces. Tracing is the operator's channel; the log is the customer's.
- `sequence` is monotone but **NOT gapless** — Postgres IDENTITY column, consumers MUST page via `sequence > after_sequence`. Do NOT introduce `MAX+1` retry loops, advisory locks, or per-task sequences; the earlier draft's "gapless" requirement was a design error.
- Dedup is **idempotency-key-based**, not sequence-based. Every write constructs `idempotency_key = sha256(task_id || branch_id || checkpoint_id || origin_ref)` and uses `INSERT ... ON CONFLICT (task_id, idempotency_key) DO NOTHING`. Implementers MUST NOT swap this for a different dedup strategy — it's what makes crash-retry and redrive safe.
- `content` shape is **versioned**. Every entry has `content_version: SMALLINT NOT NULL DEFAULT 1`. New fields in an existing `kind` bump the version. Unknown versions are rendered as a debug fold by the Console, not silently ignored.
- **Java owns the API read path; Python is write-only.** Do NOT add a `list_entries` method to the Python repository; the Java repository reads Postgres directly. No cross-service RPC for reads.
- Retention: v1 accepts unbounded per-task log growth (Tier-0-capped entries only, so per-entry size is bounded). Blob-storage offload for large `tool_result` entries is a Phase 3+ follow-up; `content_version=2` is reserved for that schema bump.

## Assumptions

- Phase 1's `tasks` table with `tenant_id` column is live; multi-tenant isolation at the DB level matches every other task-scoped table.
- Postgres `jsonb` is the existing serialization format for structured payloads (matches `checkpoints.checkpoint_payload`, `task_events.details`).
- The Console's `TaskDetailPage` supports tabs (or can accept a new Conversation pane as a sibling section). If the page is single-scroll, this task adds a collapsible section at the top.
- Polling (5 s) is acceptable; a Phase 3+ upgrade path to SSE / WebSocket streaming is out of scope.

<!-- AGENT_TASK_END: task-13-user-facing-conversation-log.md -->
