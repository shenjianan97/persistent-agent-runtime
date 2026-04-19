<!-- AGENT_TASK_START: task-13-user-facing-conversation-log.md -->

# Task 13 — User-Facing Conversation Log (separate from LangGraph checkpointer)

## Agent Instructions

Task 13 is **parallelizable across multiple subagents**. Three independent slices can proceed concurrently once the shared contracts (table schema + API response shape) are agreed in §Task-Specific Shared Contract:

- **Slice A — DB + Worker**: migration `0017`, `ConversationLogRepository` (Python), dual-write in `agent_node`, worker tests.
- **Slice B — API**: Java `ConversationLogRepository` / `ConversationLogService` / new endpoint on `TaskController`, Java tests, backend-integration E2E.
- **Slice C — Console**: `ConversationPane.tsx` + types + client helper + unit tests, `TaskDetailPage.tsx` integration, Playwright Scenario 17 spec addition in `CONSOLE_BROWSER_TESTING.md`.

Slice A must ship the migration and repository before Slice B's backend-integration E2E can run end-to-end, but both can be implemented and unit-tested in parallel. Slice C can be built against a mocked client until Slice B's endpoint is live. The docs update (`track-7-context-window-management.md`, `progress.md`) is ownerless — whichever slice lands last owns it.

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

- New table `task_conversation_log` (migration `0017_task_conversation_log.sql`):

  ```sql
  CREATE TABLE task_conversation_log (
      entry_id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
      tenant_id      TEXT NOT NULL,
      task_id        UUID NOT NULL REFERENCES tasks(task_id),
      sequence       BIGINT NOT NULL,
      kind           TEXT NOT NULL CHECK (kind IN (
          'user_turn',
          'agent_turn',
          'tool_call',
          'tool_result',
          'system_note',
          'compaction_boundary',
          'memory_flush'
      )),
      role           TEXT,                   -- 'user'|'assistant'|'tool'|'system'
      content        JSONB NOT NULL,         -- {"text": "...", "tool_name": "...", ...}
      content_size   INTEGER NOT NULL,       -- byte length of serialized content
      metadata       JSONB NOT NULL DEFAULT '{}'::jsonb,
      created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
      UNIQUE (task_id, sequence)
  );
  CREATE INDEX idx_task_conversation_log_task_seq
      ON task_conversation_log (task_id, sequence);
  CREATE INDEX idx_task_conversation_log_tenant
      ON task_conversation_log (tenant_id, created_at DESC);
  ```

- Python repository `services/worker-service/core/conversation_log_repository.py`:
  - `append_entry(task_id, tenant_id, kind, role, content, metadata) -> int` — returns the new `sequence`.
  - `list_entries(task_id, after_sequence=None, limit=None) -> list[dict]` — for the API service to consume (or the API queries Postgres directly — see §Implementation).
  - Uses the existing asyncpg pool pattern; sequence is computed as `COALESCE(MAX(sequence), 0) + 1` in a single UPSERT or acquired via an advisory lock on `(task_id)` to avoid races.

- Worker write points (in `executor/graph.py` `agent_node`):
  - **BEFORE** `compact_for_llm(raw_messages, state, agent_config, ...)`: append each NEW message in `raw_messages[state.last_super_step_message_count:]` to the log. Tool messages are appended with their full (Tier-0-capped) content.
  - **AFTER** the LLM responds and returns an `AIMessage`: append the AIMessage to the log as `kind='agent_turn'` (text) plus one `kind='tool_call'` entry per `tool_call` in the response.
  - **WHEN** Tier 3 fires (detect via `Tier3FiredEvent` in the pipeline result): append a `kind='compaction_boundary'` entry with `metadata={"summarizer_model": ..., "summary_bytes": ..., "turns_summarized": ...}` and `content={"summary_text": <summary>}`.
  - **WHEN** pre-Tier-3 memory flush fires (detect via `MemoryFlushFiredEvent`): append a `kind='memory_flush'` entry.

- New REST endpoint `GET /v1/tasks/{taskId}/conversation`:
  - Returns `{entries: [...], next_sequence: N | null}`.
  - Supports `?after_sequence=<n>&limit=<m>` for pagination and incremental-poll clients.
  - Tenant-scoped (reuses existing `TaskController` tenant guard).
  - NOT derived from checkpoints.

- Console task-detail "Conversation" pane:
  - New tab/section alongside the existing `CheckpointTimeline` (do NOT delete the timeline — it remains the source for checkpoint-level infrastructure telemetry).
  - Renders `task_conversation_log` entries in sequence order.
  - Compaction-boundary entries render as a collapsible divider. Memory-flush entries render as a single-line banner.
  - Polls every 5 s while task is active; stops when task reaches a terminal state.

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

As shown in §Task-Specific Shared Contract. The table is additive; no existing row changes. CHECK constraint on `kind` follows the Track 2 precedent (DROP+ADD pattern applies for future additions).

### Worker write path

The dual-write is inserted into `agent_node` at two sites:

1. Immediately after receiving `raw_messages` for this super-step, iterate `raw_messages[state["last_super_step_message_count"]:]` and append each as a log entry. Do NOT re-append messages already logged (the watermark makes this deterministic).
2. After the LLM response returns, append the response as `agent_turn` plus one `tool_call` per `response.tool_calls[*]`.
3. After `compact_for_llm` returns events, iterate and append `compaction_boundary` for any `Tier3FiredEvent`, `memory_flush` for any `MemoryFlushFiredEvent`.

Repository signature:

```python
class ConversationLogRepository:
    async def append_entry(
        self,
        *,
        task_id: str,
        tenant_id: str,
        kind: Literal["user_turn","agent_turn","tool_call","tool_result",
                      "system_note","compaction_boundary","memory_flush"],
        role: str | None,
        content: dict,
        metadata: dict | None = None,
    ) -> int: ...
    async def list_entries(
        self,
        *,
        task_id: str,
        after_sequence: int | None = None,
        limit: int | None = None,
    ) -> list[dict]: ...
```

Sequence assignment via `INSERT ... VALUES (..., COALESCE((SELECT MAX(sequence)+1 FROM task_conversation_log WHERE task_id=$1), 1), ...)` wrapped in a single statement (Postgres serialises it under the `UNIQUE(task_id, sequence)` constraint; retry on 23505 with bounded attempts if needed). An advisory lock keyed on `hashtext(task_id::text)` is an acceptable alternative.

**Failure mode:** if the log append fails (DB down), log a structured warning and continue. The task MUST NOT dead-letter just because the audit log is unavailable — model execution is the critical path. Best-effort semantics.

### API endpoint

`GET /v1/tasks/{taskId}/conversation?after_sequence={N}&limit={M}`:

- Response shape (Java record `ConversationEntryResponse`):
  ```json
  {
    "entries": [
      {
        "sequence": 1,
        "kind": "user_turn",
        "role": "user",
        "content": {"text": "Do the thing"},
        "metadata": {},
        "created_at": "2026-04-19T..."
      },
      {
        "sequence": 2,
        "kind": "tool_call",
        "role": "assistant",
        "content": {"tool_name": "sandbox_read_file", "args": {"path": "/..."}, "call_id": "..."},
        "metadata": {}
      },
      {
        "sequence": 3,
        "kind": "compaction_boundary",
        "role": "system",
        "content": {"summary_text": "Earlier: the agent explored /tmp and found 14 log files..."},
        "metadata": {"summarizer_model": "claude-haiku-4-5", "turns_summarized": 18, "summary_bytes": 412}
      }
    ],
    "next_sequence": null
  }
  ```
- `next_sequence` is non-null when the page was truncated by `limit`; clients re-request with `after_sequence=next_sequence`.
- Default `limit=500`; max `limit=2000`.
- Tenant check via existing `TaskController` tenant guard.
- Returns 404 if task doesn't exist or belongs to another tenant.

### Console "Conversation" pane

- Add a second tab to `TaskDetailPage` labelled "Conversation" (next to the existing Timeline tab).
- Component `ConversationPane` polls `/v1/tasks/{id}/conversation?after_sequence={lastSeq}` every 5 s while task is non-terminal.
- Entry rendering by `kind`:
  - `user_turn` — user bubble (left-aligned or styled as user input).
  - `agent_turn` — assistant bubble.
  - `tool_call` — collapsed card showing tool name + args preview; click to expand.
  - `tool_result` — collapsed card with head of output + "expand" to see full; head/tail if the result was Tier-0 capped.
  - `compaction_boundary` — inline divider: "— Context summarized at this point —" with a caret that expands to show `content.summary_text` and `metadata.summarizer_model / turns_summarized / summary_bytes`.
  - `memory_flush` — single-line system banner: "— Memory flush fired (one-shot) —".
- The existing `CheckpointTimeline` pane is unchanged — it continues to surface checkpoint counts, cost, HITL events, redrives, and tier-event structured logs for operator debugging. The two panes serve different audiences.

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

- [ ] Migration `0017_task_conversation_log.sql` applies cleanly on a fresh DB and on an existing DB with pre-existing tasks (no row churn).
- [ ] Worker appends exactly one entry per new user/tool/agent message per super-step. No duplicates on redrive (watermark-gated).
- [ ] Tier 3 firing produces exactly one `compaction_boundary` entry per firing; the entry's `content.summary_text` equals the generated summary; `metadata.turns_summarized` matches the `Tier3FiredEvent` payload.
- [ ] Pre-Tier-3 memory flush produces exactly one `memory_flush` entry per task (matches the one-shot invariant).
- [ ] Tier 1 / Tier 1.5 firings do NOT produce any conversation-log entries (they're invisible per design).
- [ ] Per-tool-result cap still applies to conversation-log entries (the log does not store multi-MB outputs — 25KB is the ceiling).
- [ ] `GET /v1/tasks/{taskId}/conversation` returns entries in sequence order; pagination via `after_sequence` works; tenant isolation enforced.
- [ ] Console Conversation pane renders all seven `kind` values correctly; polling stops at terminal state.
- [ ] Console CheckpointTimeline pane still renders its existing checkpoint/HITL/redrive events — no regression.
- [ ] Task with a forced Tier 3 firing renders an expandable compaction-boundary divider; expanding shows the summary text.
- [ ] If the conversation-log append fails (simulated DB error on the log write path), the task still completes successfully; a structured warning is logged.
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
- Do NOT fail a task on conversation-log write failure — best-effort semantics.
- Do NOT inline `summary_text` into the `messages` channel in state (that's already done by Task 8's summarizer). The conversation log's `compaction_boundary.content.summary_text` MAY duplicate the summary text — that's intentional (the log is self-contained for audit export).
- Do NOT expose the conversation-log write path to customer tools or MCP servers. It's platform-owned.
- Do NOT batch conversation-log writes across super-steps. Append per-message, synchronously, within the super-step boundary.
- Do NOT add conversation-log content to Langfuse traces. Tracing is the operator's channel; the log is the customer's.
- Sequence allocation: one statement `INSERT ... (SELECT COALESCE(MAX(sequence)+1, 1) ...)` with retry on 23505, OR `pg_advisory_xact_lock(hashtext(task_id))` at transaction start. Do NOT use a sequence object — sequences don't guarantee gapless ordering under aborted transactions, and the UI's pagination relies on contiguous sequences.

## Assumptions

- Phase 1's `tasks` table with `tenant_id` column is live; multi-tenant isolation at the DB level matches every other task-scoped table.
- Postgres `jsonb` is the existing serialization format for structured payloads (matches `checkpoints.checkpoint_payload`, `task_events.details`).
- The Console's `TaskDetailPage` supports tabs (or can accept a new Conversation pane as a sibling section). If the page is single-scroll, this task adds a collapsible section at the top.
- Polling (5 s) is acceptable; a Phase 3+ upgrade path to SSE / WebSocket streaming is out of scope.

<!-- AGENT_TASK_END: task-13-user-facing-conversation-log.md -->
