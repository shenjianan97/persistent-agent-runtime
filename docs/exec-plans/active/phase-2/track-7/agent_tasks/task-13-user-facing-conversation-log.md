<!-- AGENT_TASK_START: task-13-user-facing-conversation-log.md -->

# Task 13 — User-Facing Conversation Log (separate from LangGraph checkpointer)

## Agent Instructions

Task 13 is **parallelizable across three independent subagents**. The shared contract (§Task-Specific Shared Contract — schema, `content` schema per `kind`, API response shape, ownership split) is fully specified so slices don't need to coordinate mid-flight.

- **Slice A — DB + Worker**: migration `0017`, `ConversationLogRepository` (Python, append-only), dual-write in `agent_node` including idempotency key handling + `HumanMessage.id` fallback + HITL hookups, worker unit + DB tests. **Slice A also owns `progress.md` updates** (add Task 13 row, mark Done on final merge). This avoids three-way merge conflicts on that file.
- **Slice B — API**: Java `ConversationLogRepository` (read-only), `ConversationLogService`, new endpoint on `TaskController`, Java unit + serialization tests, backend-integration E2E (runs against Slice A's migration — Slice A merges first OR Slice B stubs the repo behind a feature flag until A lands).
- **Slice C — Console**: `ConversationPane.tsx` + types + `client.ts` helper + unit tests against a mocked fixture of the §API endpoint response shape. `TaskDetailPage.tsx` integration (tab reconciliation). Playwright Scenario 17 added to `CONSOLE_BROWSER_TESTING.md`.

**Ownership rules (non-negotiable — these keep slices independent):**
- The Python repository is **append-only** (`append_entry` only). It has NO `list_entries`, no `mark_superseded`, no update methods. Slice A MUST NOT add any.
- The Java repository is **read-only**. It reads Postgres directly. Slice B MUST NOT call into Slice A's Python code.
- The `content` shape per `kind` and the API response JSON are authoritative in §Content schema + §API endpoint. Neither slice may deviate.
- Idempotency key format is authoritative: `sha256(task_id || (checkpoint_id or "init") || origin_ref)`. Origin ref rules are specified in §Worker write path; `HumanMessage.id` nullability handling is also specified there.
- Tenant-isolation contract is authoritative in §API read path: Java `findByTask` MUST filter by `(tenant_id, task_id)` resolved from the authenticated principal. Slice B MUST NOT accept a client-supplied `tenant_id`.
- The CHECK-constraint name `chk_task_conversation_log_kind` is authoritative for any future `kind` additions (Track 2 DROP+ADD pattern).

**Merge order:** Slice A merges first (defines the schema + `progress.md` row). Slices B and C can merge in either order after that. The design-doc update (`track-7-context-window-management.md` §Customer-visible behavior changes) is also owned by Slice A to avoid cross-slice doc conflicts.

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
    task_id          UUID NOT NULL,
    -- Monotone ordering — NOT gapless. Consumers must page via `sequence > N`,
    -- never assume a contiguous 1..N range (gaps arise from rare insert
    -- retries). Postgres IDENTITY gives a single source of truth with no
    -- MAX+1 race and no advisory lock.
    sequence         BIGINT GENERATED ALWAYS AS IDENTITY,
    -- LangGraph checkpoint this entry was produced in (for cross-ref with
    -- the checkpointer and for dedup). NULL only for `system_note` entries
    -- that are not tied to a specific super-step.
    checkpoint_id    TEXT,
    -- Idempotency key — `sha256(task_id || checkpoint_id || origin_ref)`
    -- where origin_ref is the LangGraph message id for model/tool messages
    -- or a deterministic compaction-event id (e.g.,
    -- "tier3:<watermark_before>->{watermark_after}") for compaction entries.
    -- A duplicate insert with the same key is a no-op (ON CONFLICT DO NOTHING)
    -- — this makes every worker write idempotent across retries and crashes.
    idempotency_key  TEXT NOT NULL,
    kind             TEXT NOT NULL,
    role             TEXT,                  -- 'user' | 'assistant' | 'tool' | 'system'
    content_version  SMALLINT NOT NULL DEFAULT 1,   -- bumped on schema change
    content          JSONB NOT NULL,                 -- shape per-kind (see §Content schema)
    content_size     INTEGER NOT NULL,               -- serialized bytes; surfaces "truncated" copy in Console and feeds ops dashboards without an expensive jsonb scan
    metadata         JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- Composite FK enforces tenant_id consistency with tasks(task_id, tenant_id).
    -- Prevents a bug (or malicious worker) from appending a log entry with a
    -- tenant_id that doesn't match the task's owner. Matches the multi-tenant
    -- integrity pattern used elsewhere in this repo.
    CONSTRAINT fk_task_conversation_log_task
        FOREIGN KEY (task_id, tenant_id)
        REFERENCES tasks (task_id, tenant_id)
        ON DELETE CASCADE,
    CONSTRAINT chk_task_conversation_log_kind
        CHECK (kind IN (
            'user_turn',
            'agent_turn',
            'tool_call',
            'tool_result',
            'system_note',
            'compaction_boundary',
            'memory_flush',
            'hitl_pause',
            'hitl_resume'
        )),
    UNIQUE (task_id, idempotency_key)
);

CREATE INDEX idx_task_conversation_log_task_seq
    ON task_conversation_log (task_id, sequence);
```

**Schema rationale:**

- `ON DELETE CASCADE` on the composite FK means a `DELETE FROM tasks WHERE task_id = $1` cascades to the log — this is the v1 right-to-delete hook (tenant offboarding or GDPR-initiated task purge flows through the existing task-delete path).
- Composite FK `(task_id, tenant_id) REFERENCES tasks(task_id, tenant_id)` requires the `tasks` table to have a `UNIQUE (task_id, tenant_id)` or `UNIQUE (tenant_id, task_id)` index. Migration `0017` MUST add that unique index to `tasks` if one doesn't already exist (task_id is already primary key so the pair is trivially unique — just materialize the composite index).
- `CHECK` constraint has an explicit name (`chk_task_conversation_log_kind`) so future `hitl_pause`/`hitl_resume`/other additions follow the Track 2 DROP+ADD pattern.
- `UNIQUE(task_id, idempotency_key)` is a full (not partial) constraint so dedup applies to every row.
- Single index on `(task_id, sequence)` covers the hot read path `WHERE task_id = $1 AND sequence > $2 ORDER BY sequence LIMIT $3`. Dropped the `(tenant_id, created_at)` index — nothing reads on that predicate in v1.
- `content_size` is an explicit column (not derived) so the Console can cheaply render "Tool returned N bytes" copy and ops dashboards can aggregate log volume without a jsonb scan.

**Redrive / rollback is explicitly out of scope for v1.** `git grep` for `rollback|redrive` in `services/worker-service` returns zero call sites today; there is no rollback API to hook into. When Phase 3+ ships a rollback API, it will add a migration that introduces either (a) a `superseded_at TIMESTAMPTZ` column + `mark_superseded()` mutation, or (b) a `branch_id UUID` column + per-branch idempotency keys. `content_version=2` is RESERVED for that migration. v1 workers write only crash-retry-safe entries; a rollback simply appends a fresh continuation that will appear after earlier entries in the pane. Customers will see the full history including the pre-rollback continuation — acceptable because there is no rollback UX surfaced to customers in Phase 2.

### Content schema per `kind` (v1)

Content is a JSONB blob whose shape is a function of `kind`. Every entry also has `content_version` (int, default 1). Implementers MUST NOT diverge from these shapes; new fields require bumping `content_version`.

| `kind` | `content` shape | `metadata` shape |
|--------|-----------------|------------------|
| `user_turn` | `{"text": str}` | `{}` |
| `agent_turn` | `{"text": str}` — text portion of the AIMessage | `{"message_id": str, "finish_reason": str \| null}` |
| `tool_call` | `{"tool_name": str, "args": object, "call_id": str}` — one entry per `AIMessage.tool_calls[*]` | `{"message_id": str}` |
| `tool_result` | `{"call_id": str, "tool_name": str, "text": str, "is_error": bool}` — `text` is Tier-0-capped (≤ 25KB) | `{"orig_bytes": int, "capped": bool}` |
| `system_note` | `{"text": str}` — platform-owned notes (rarely used in v1) | `{}` |
| `compaction_boundary` | `{"summary_text": str, "first_turn_index": int, "last_turn_index": int}` | `{"summarizer_model": str, "turns_summarized": int, "summary_bytes": int, "cost_microdollars": int, "tier3_firing_index": int}` |
| `memory_flush` | `{}` | `{}` |
| `hitl_pause` | `{"reason": str, "prompt_to_user": str \| null}` — reason is the HITL trigger (e.g., `"tool_requires_approval"`, `"agent_requested"`); Console renders as inline banner | `{"checkpoint_id": str, "tool_name": str \| null}` |
| `hitl_resume` | `{"resolution": str, "user_note": str \| null}` — resolution is `"approved"`, `"rejected"`, `"modified"`, or `"cancelled"` | `{"resolved_by": str, "resolved_at": str}` |

**`tool_call.args` serialization contract:** Python writes args via `json.dumps(args, default=str)` to handle non-JSON-native values (datetime, Decimal, bytes, Path). Java reads args as an opaque JSON `object` and passes through to the Console. Console renders args as a pretty-printed JSON blob — does NOT attempt type reconstruction.

Unknown-field behavior on the API read path: Java unmarshals via Jackson with `FAIL_ON_UNKNOWN_PROPERTIES=false` on `content`/`metadata` only, so a schema-v2 entry served to a schema-v1 Console degrades gracefully. Console renders any unrecognized `kind` as a neutral "system event" banner with raw JSON in a debug fold.

### Worker write path

Dual-write happens inside `agent_node` — NEVER inside `compact_for_llm` (pipeline stays pure). Write points:

1. **Pre-LLM turns** — append entries for any NEW user/tool messages in the super-step. Anchor precisely: slice off `state["messages"]` (NOT the locally-prepended `messages` variable that carries SystemMessage), using `state["messages"][state.last_super_step_message_count:]` to find just this super-step's additions. SystemMessages MUST NOT enter the log — they are platform/agent config, not user-facing conversation. Each entry's `idempotency_key = sha256(task_id || (checkpoint_id or "init") || message_id_or_fallback)`. `checkpoint_id` comes from the config passed to `agent_node`; when `None` (pre-Task-10 state), substitute the literal `"init"` so the first-turn keys are stable.
2. **LLM response** — append the response as `agent_turn` (text content only), plus one `tool_call` per `response.tool_calls[*]`. Same idempotency-key scheme using `response.id` and each `tool_call["id"]`.
3. **Compaction events** — iterate `PassResult.events` returned by `compact_for_llm`. For each `Tier3FiredEvent`: append `compaction_boundary` with `content.first_turn_index` / `last_turn_index` populated from the event payload. For each `MemoryFlushFiredEvent`: append `memory_flush`. Idempotency key uses `f"tier3:{watermark_before}->{watermark_after}"` or `f"flush:{checkpoint_id}"` — stable across retries.
4. **HITL events** — pause/resume appends are issued from the existing HITL handler code paths (not from `agent_node`). `hitl_pause` is appended when the graph enters an interrupt; `hitl_resume` is appended when the interrupt is resolved. Idempotency key uses `f"hitl_pause:{checkpoint_id}"` and `f"hitl_resume:{checkpoint_id}"` — a pause/resume cycle on a given checkpoint produces exactly one of each.

**LangChain message ID nullability:** fresh `HumanMessage(content=...)` construction leaves `msg.id is None` (only round-tripped Anthropic messages get server-assigned IDs). To avoid `sha256(...|| None)` collapsing all first-turn entries to one key, the worker MUST:

```python
message_id = msg.id if msg.id is not None else f"seed:{uuid4()}"
```

The `seed:` prefix documents intent. The generated UUID is deterministic per super-step attempt (same HumanMessage object), so crash-retry still dedups correctly via `ON CONFLICT DO NOTHING`.

**Write ordering guarantee:** appends happen BEFORE the LangGraph super-step commits its checkpoint. If the checkpoint commit fails, the next super-step retry re-runs; same `checkpoint_id` + same message IDs → same idempotency keys → `ON CONFLICT DO NOTHING` prevents duplicates.

**Transaction boundaries:** each log append is its own statement (no outer transaction around the super-step's log + checkpoint write). Intentional — LangGraph's checkpointer owns its own transaction, the log is best-effort audit.

**Failure envelope (best-effort semantics).** When an append fails (connection reset, DB unavailable, etc.), the worker MUST:

1. Log at WARN level via the structured logger `conversation_log.append_failed` with required fields: `task_id`, `tenant_id`, `checkpoint_id`, `idempotency_key`, `kind`, `exception_class`, `exception_message` (truncated to 500 chars).
2. Increment the Prometheus/metric counter `conversation_log_append_failed_total` labeled by `kind` and `exception_class`.
3. NOT raise — the super-step proceeds. Idempotency-key replay on the next super-step attempt recovers the entry.

A sustained spike on `conversation_log_append_failed_total` is the ops signal that the log store is degraded; without it the silent-fail posture is invisible at fleet scale.

### API read path

**Java owns the read path.** The Python `ConversationLogRepository` is write-only. The API service has its own Java `ConversationLogRepository` reading directly from Postgres — no cross-service RPC.

`GET /v1/tasks/{taskId}/conversation?after_sequence={N}&limit={M}`:

- Default `limit=200`; max `limit=1000`. (Lowered from the earlier draft after Codex review — large pages are unnecessary given 5s polling.)
- Pagination: `next_sequence` is the max `sequence` of the returned page when `len(entries) == limit`, else `null`.
- 404 when task doesn't exist or belongs to another tenant.

**Tenant-isolation contract (explicit — NOT implicit):** The Java `ConversationLogRepository.findByTask` query MUST read:

```sql
SELECT ... FROM task_conversation_log
 WHERE task_id = :taskId
   AND tenant_id = :tenantId     -- authoritative; resolved from authenticated principal
   AND sequence > :afterSequence
 ORDER BY sequence
 LIMIT :limit
```

`tenantId` is resolved by the service layer from the authenticated principal / request context via the existing mechanism used by other tenant-scoped endpoints in `TaskController`. The client NEVER provides `tenant_id`. If the `(task_id, tenant_id)` row doesn't exist, the endpoint returns 404 (indistinguishable from "task does not exist") — never 403. This prevents task-id enumeration oracles across tenants.

**Cross-tenant leakage via compaction summaries (explicit invariant):** `compaction_boundary.content.summary_text` is generated by `summarize_slice()` which operates on a single task's `state["messages"]` slice — never across tasks or tenants. Implementers MUST NOT change this contract.

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

As shown in §Task-Specific Shared Contract. Table is additive; no existing row changes; runs online (no lock on existing rows). The migration MUST also ensure `tasks(task_id, tenant_id)` has a unique index — if not already present via the `tasks.task_id` primary key's interaction with the `tenant_id` column, add `CREATE UNIQUE INDEX IF NOT EXISTS uq_tasks_task_tenant ON tasks (task_id, tenant_id);` as a prerequisite. Named CHECK constraint `chk_task_conversation_log_kind` follows the Track 2 DROP+ADD pattern for future additions. `content_version` starts at 1; schema evolution bumps it.

### Redrive / rollback (out of scope for v1)

`git grep -n "rollback\|redrive" services/worker-service` returns zero call sites today. There is no rollback API in the codebase, so v1 does not implement rollback handling.

**What v1 does:**
- Crash-retry dedup via `ON CONFLICT DO NOTHING` — same `checkpoint_id` + same `message_id` → same idempotency key → second write is a no-op.
- No `mark_superseded`, no `superseded_at` column, no `branch_id`.

**What Phase 3+ will do (NOT implemented here):** when a rollback API ships, it adds a migration that introduces either `superseded_at TIMESTAMPTZ` (soft-delete model) or `branch_id UUID` (branching model) and bumps `content_version` to 2. The phase-3 task also backfills a migration path for v1 rows (no backfill needed for soft-delete; branching backfills each task's entries with a single synthetic branch id).

Until then, if a rollback operation somehow lands (manual DB intervention, future work not coordinated with Task 13), the Conversation pane simply shows the pre-rollback entries followed by the post-rollback continuation in `sequence` order. Customers have no rollback UX surfaced in Phase 2 so this will not manifest.

### Worker ownership & repository

**Python side — append-only.** `services/worker-service/core/conversation_log_repository.py`:

```python
class ConversationLogRepository:
    async def append_entry(
        self,
        *,
        task_id: str,
        tenant_id: str,
        checkpoint_id: str | None,
        idempotency_key: str,
        kind: Literal[
            "user_turn", "agent_turn", "tool_call", "tool_result",
            "system_note", "compaction_boundary", "memory_flush",
            "hitl_pause", "hitl_resume",
        ],
        role: str | None,
        content: dict,
        metadata: dict | None = None,
    ) -> int | None:
        """Insert one entry. Returns assigned `sequence`, or None if the
        idempotency key already existed (ON CONFLICT DO NOTHING swallowed it).

        On any database error, logs via `conversation_log.append_failed`,
        increments `conversation_log_append_failed_total` counter, and
        returns None. Never raises — callers treat None as "maybe wrote,
        maybe dedup'd, maybe failed — don't depend on this".
        """
```

Python MUST NOT expose a `list_entries` method; the API reads Postgres directly (see next).

**Java side — read only.** `services/api-service/src/main/java/com/persistentagent/api/repository/ConversationLogRepository.java` with a single method:

```java
List<ConversationEntryRow> findByTask(
    UUID tenantId,
    UUID taskId,
    long afterSequence,
    int limit
);
```

The query always filters by `(tenant_id, task_id)` — tenant scoping is NOT optional. No cross-service calls. The two repositories speak to the same table from opposite directions.

### API endpoint

`GET /v1/tasks/{taskId}/conversation?after_sequence={N}&limit={M}`:

- Response shape (Java record `ConversationEntryResponse`):
  ```json
  {
    "entries": [
      {
        "sequence": 101,
        "kind": "user_turn",
        "role": "user",
        "content_version": 1,
        "content": {"text": "Do the thing"},
        "metadata": {},
        "content_size": 14,
        "created_at": "2026-04-19T..."
      },
      {
        "sequence": 102,
        "kind": "tool_call",
        "role": "assistant",
        "content_version": 1,
        "content": {"tool_name": "sandbox_read_file", "args": {"path": "/..."}, "call_id": "call_abc"},
        "metadata": {"message_id": "ai_xyz"},
        "content_size": 87
      },
      {
        "sequence": 140,
        "kind": "compaction_boundary",
        "role": "system",
        "content_version": 1,
        "content": {"summary_text": "Earlier: the agent explored /tmp and found 14 log files...", "first_turn_index": 5, "last_turn_index": 22},
        "metadata": {"summarizer_model": "claude-haiku-4-5", "turns_summarized": 18, "summary_bytes": 412, "cost_microdollars": 238, "tier3_firing_index": 1},
        "content_size": 448
      }
    ],
    "next_sequence": null
  }
  ```
- `next_sequence = max(sequence)` when `len(entries) == limit`, else `null`.
- Default `limit=200`; max `limit=1000`.
- Tenant guard: see §"Tenant-isolation contract" in §API read path — NOT optional.
- 404 when task doesn't exist or belongs to another tenant (indistinguishable).

### Console "Conversation" pane

**Tab structure on `TaskDetailPage`:**
- **Conversation** (default tab) — subtitle "What the agent did". Customer audience.
- **Timeline** (existing `CheckpointTimeline`, unchanged) — subtitle "Infrastructure events". Operator audience.
- Deep-link preserved via `/tasks/:id?tab=conversation` or `?tab=timeline`; absent param defaults to `conversation`.

**Polling:** `GET /v1/tasks/{id}/conversation?after_sequence={lastSeq}` every 5 s while task status is non-terminal. Stops on terminal state.

**"New activity" affordance:** When the pane has been scrolled away from the tail and new entries arrive, render a sticky pill at the bottom: "N new entries ↓". Clicking scrolls to the new tail. Standard chat-UI pattern.

**Entry rendering by `kind`:**
- `user_turn` — user bubble.
- `agent_turn` — assistant bubble.
- `tool_call` — collapsed card: tool name + args preview; expandable to pretty-printed JSON of `content.args`.
- `tool_result` — collapsed card: head of `content.text` + "Expand". When `metadata.capped=true`, the card shows the explicit copy: **"Tool returned {metadata.orig_bytes} bytes; showing head+tail capped at 25KB (same view the model had)."** This removes the "is the AI missing data?" ambiguity — the capped form IS what the model saw.
- `compaction_boundary` — expandable inline divider: **"— Context summarized (turns {first_turn_index}–{last_turn_index}, {metadata.turns_summarized} turns) —"**. Expanded view shows `summary_text` (primary), with a secondary operator-only fold containing `summarizer_model`, `summary_bytes`, `cost_microdollars`, `tier3_firing_index`. Customers see the what (turns summarized, summary); operators see the how (model, cost).
- `memory_flush` — single-line system banner: "— Memory note injected —".
- `hitl_pause` — inline banner: **"⏸ Paused awaiting human approval: {content.reason}"**. If `content.prompt_to_user` is set, render it below.
- `hitl_resume` — inline banner: **"▶ Resumed: {content.resolution}"** with optional `content.user_note` below.
- `system_note` — neutral inline banner.
- Unknown `kind` or `content_version > 1` — neutral "system event" banner with raw JSON in a debug fold.

**HITL context:** `hitl_pause` / `hitl_resume` are first-class kinds so customers watching a task see WHY it paused (not a silent hang). The Timeline pane also carries HITL events in its native form (matching `CheckpointTimeline`'s existing `HITL_EVENT_TYPES`); the two representations are complementary — Conversation surfaces the customer-meaningful pause moment, Timeline surfaces the full checkpoint/resolution audit.

### Retention (v1 → Phase 3+ follow-up)

The log stores Tier-0-capped tool results (≤ 25KB each) in `jsonb`. A long-running task with hundreds of tool calls can produce multi-MB log footprints per task.

**v1 posture:** accept unbounded per-task growth, bounded per-entry by the Tier 0 cap. Track 3's per-task budget (token/USD cap) is the backstop against runaway log flooding — a task that loops long enough to write 10K log rows first hits the budget ceiling and dead-letters. `make e2e-test` includes a fixture confirming Postgres handles realistic volume (hundreds of rows per task, tens of MB total, sub-100ms queries per page).

**Operational note:** at fleet scale (~10K tasks/day × ~200 rows × ~5KB avg = ~10GB/day), the table dominates DB footprint within weeks. Partitioning by `created_at` range (or task creation window) is tracked alongside the blob-offload follow-up below.

**Right-to-delete:** `ON DELETE CASCADE` on the composite FK means `DELETE FROM tasks WHERE task_id = $1` cascades to the log. Customer- and compliance-initiated deletions flow through the existing task-delete path; no separate conversation-log delete endpoint is needed in v1. Tenant offboarding deletes tasks, which cascades to logs.

**Phase 3+ follow-ups (deferred, NOT in scope of Task 13):** three schema-v2 enhancements reserved behind `content_version=2`:
1. **Blob offload for large `tool_result` entries** — store a 2KB preview in `content.text` and a pointer into the existing `task_artifacts` table (`content.artifact_s3_key`). Console renders the preview + a "Load full result" button.
2. **Rollback / supersede OR branching** — add a rollback API to the worker; paired with either `superseded_at TIMESTAMPTZ` (soft-delete) or `branch_id UUID` (branching) column, plus an audit-log row per rollback operation.
3. **Customer-facing export** — JSON / Markdown download of the Conversation pane. Genuinely useful for audit, but sibling task not Task 13 scope.
4. **Time-range partitioning** — when per-tenant storage becomes the dominant cost, partition by `created_at` month and enable archive-drop of older partitions.

### Docs update

`docs/design-docs/phase-2/track-7-context-window-management.md` §"Customer-visible behavior changes" — rewrite to reflect the new invariant:

> ### Customer-visible behavior changes (v2, per Task 13)
>
> Compaction is invisible to the customer by default. The Console's task-detail Conversation pane reads from a separate append-only `task_conversation_log` table, not from LangGraph checkpoints. Customers see:
>
> - **Tier 0 per-result cap (25KB head+tail):** applied at ingestion; the conversation log stores the same capped form the model sees. The Console makes this explicit via capped-result copy so customers know the AI saw exactly what they see.
> - **Tier 1 (tool-result clearing):** INVISIBLE. The conversation log retains the full Tier-0-capped tool result; Tier 1 only affects what the model is shown.
> - **Tier 1.5 (tool-call arg truncation):** INVISIBLE.
> - **Tier 3 (retrospective summarization):** VISIBLE. A `compaction_boundary` entry is appended to the conversation log with the generated summary and turn range. The Console renders this as an expandable inline divider with turn-count copy; messages above and below remain fully visible.
> - **Pre-Tier-3 memory flush:** VISIBLE. A single-line banner indicates the one-shot was inserted.
> - **HITL pause/resume:** VISIBLE via the existing HITL handler emitting `hitl_pause` / `hitl_resume` entries with reason and resolution.
>
> The LangGraph checkpoint remains the source of truth for the model's view. The conversation log is best-effort audit data; an append failure logs WARN and increments a counter but does not fail the task. Rollback/redrive is Phase 3+.

## Acceptance Criteria

- [ ] Migration `0017_task_conversation_log.sql` applies cleanly on a fresh DB and on an existing DB with pre-existing tasks (no row churn). Columns and constraints match §Schema exactly: composite FK `(task_id, tenant_id) REFERENCES tasks(task_id, tenant_id) ON DELETE CASCADE`, named CHECK constraint `chk_task_conversation_log_kind` covering all 9 kinds, `sequence BIGINT GENERATED ALWAYS AS IDENTITY`, `UNIQUE(task_id, idempotency_key)`. NO `superseded_at` or `branch_id` columns (both Phase 3+).
- [ ] Deleting a row from `tasks` cascades to `task_conversation_log` (verified by test).
- [ ] Worker appends exactly one entry per new user/tool/agent message per super-step. Retrying the same super-step (same `checkpoint_id`, same message id) produces ZERO new rows (ON CONFLICT DO NOTHING exercised by the test).
- [ ] Fresh `HumanMessage` with `id=None` produces a stable idempotency key derived from a generated `seed:uuid4()` fallback; retrying the same super-step reuses the same message object so dedup still works.
- [ ] Tier 3 firing produces exactly one `compaction_boundary` entry per firing; `content.summary_text` equals the generated summary; `content.first_turn_index` / `last_turn_index` populated; `metadata.turns_summarized` matches the `Tier3FiredEvent` payload.
- [ ] Pre-Tier-3 memory flush produces exactly one `memory_flush` entry per task per checkpoint.
- [ ] Tier 1 / Tier 1.5 firings do NOT produce any conversation-log entries.
- [ ] HITL pause and resume each produce exactly one entry (`hitl_pause`, `hitl_resume`) with correct `content.reason` / `content.resolution`.
- [ ] Per-tool-result cap still applies to conversation-log `tool_result` entries (≤ 25KB; `metadata.capped=true` + `metadata.orig_bytes` set when truncated at ingestion).
- [ ] `GET /v1/tasks/{taskId}/conversation` returns entries with `sequence > after_sequence`. Pagination `next_sequence = max(sequence)` when page is full, else `null`. Tenant isolation enforced: a task owned by tenant B returns 404 when requested by tenant A (never 403 — no enumeration oracle).
- [ ] The Java SQL query includes `WHERE tenant_id = :tenantId AND task_id = :taskId` (verified by reading the repository code).
- [ ] `content` shapes per `kind` match §Content schema exactly (test asserts Jackson deserialises every `kind` into its documented shape).
- [ ] `content_version=1` for every v1 entry; a deliberately injected entry with `content_version=2` is still served by the API and rendered as a debug-fold by the Console.
- [ ] Console Conversation pane renders all 9 `kind` values correctly; polling stops at terminal state.
- [ ] Console CheckpointTimeline pane still renders its existing checkpoint/HITL events — no regression.
- [ ] TaskDetailPage defaults to the Conversation tab; `?tab=timeline` deep-link selects Timeline.
- [ ] Task with a forced Tier 3 firing renders an expandable compaction-boundary divider with the turn-range copy; expanding shows the summary text with operator-fold for model/cost metadata.
- [ ] Capped `tool_result` cards render the explicit "Tool returned N bytes; capped at 25KB (same view the model had)" copy when `metadata.capped=true`.
- [ ] If the conversation-log append fails (simulated DB error), the task still completes successfully; `conversation_log.append_failed` logged at WARN with all required fields; `conversation_log_append_failed_total` counter incremented.
- [ ] If the LangGraph checkpoint commit fails AFTER log entries landed, the super-step retry re-emits the same message ids and the ON CONFLICT path prevents duplicates.
- [ ] `make worker-test`, `make api-test`, `make console-test`, `make e2e-test` all pass.
- [ ] Orchestrator Playwright Scenario 17 passes.

## Testing Requirements

- **Worker DB test** (`test_conversation_log_repository.py`): insert + idempotency-key dedup (second insert with same key is a no-op, returns `None`), concurrent appends (10 parallel writes produce 10 rows with monotone — not necessarily contiguous — sequences), composite-FK tenant integrity (appending with mismatched `(task_id, tenant_id)` fails), ON DELETE CASCADE (deleting a task row purges its log rows), failure envelope (DB connection broken → WARN log emitted, counter incremented, no raise).
- **Worker integration test** (`test_conversation_log_integration.py`): run a synthetic graph where Tier 3 fires; assert the log contains user + agent + tool entries plus a `compaction_boundary` entry with correct metadata (including `first_turn_index` / `last_turn_index`); assert Tier 1 firing does NOT add any log entries; assert SystemMessage is NOT in the log. Add a HITL scenario: trigger a pause, assert `hitl_pause` entry; resume, assert `hitl_resume` entry.
- **API unit test**: endpoint shape, pagination, tenant isolation (explicit: request a task owned by tenant B as tenant A → 404, not 403, not leak). Verify Jackson round-trip for every `kind` (including `hitl_pause`/`hitl_resume`).
- **Backend-integration E2E** (`test_conversation_log_endpoint.py`): `POST /v1/tasks` → wait until complete → `GET /v1/tasks/{id}/conversation` → assert entries count and shape. Second test: create tasks for two tenants, verify tenant A cannot read tenant B's conversation.
- **Console unit test** (`ConversationPane.test.tsx`): renders each `kind`; expand/collapse on `compaction_boundary`; capped-result explicit copy; HITL banners; polling stop on terminal status; "N new entries" pill when scrolled; error boundary on network failure; `content_version=2` renders debug fold.
- **Playwright Scenario 17** (`CONSOLE_BROWSER_TESTING.md`): authoritative setup steps —
  1. Create an agent via Console with `summarizer_model=claude-haiku-4-5` and `context_management.tier3_trigger_fraction=0.1` (abnormally low to force Tier 3 quickly).
  2. Submit a task whose initial prompt asks the agent to run `sandbox_read_file` repeatedly on a fixture file `/tmp/large_log.txt` that has been pre-seeded with ~30KB content (so each tool result is capped to 25KB and consumes context fast).
  3. Watch the Conversation tab poll; assert within 60s a `compaction_boundary` divider appears.
  4. Click the divider; assert the summary text expands and the operator-fold is present but collapsed by default.
  5. Assert tool-result cards above the divider are still expandable and show the capped-result copy.
  6. Click the Timeline tab; assert it still renders checkpoint events unchanged (no regression).

## Constraints and Guardrails

- Do NOT derive the Conversation pane from LangGraph checkpoints. The separation is the whole point.
- Do NOT remove or rename the existing `CheckpointTimeline` — it serves a different purpose (operator/infra view).
- Do NOT apply Tier 1 / 1.5 / 3 transforms to the conversation log. Tier 0 (per-result cap) still applies — the log is NOT a raw-binary blob store.
- Do NOT fail a task on conversation-log write failure — best-effort semantics. Log a structured warning and continue.
- Do NOT inline `summary_text` into the `messages` channel in state (that's already done by Task 8's summarizer). The conversation log's `compaction_boundary.content.summary_text` MAY duplicate the summary — intentional (log is self-contained for export).
- Do NOT expose the conversation-log write path to customer tools or MCP servers. Platform-owned.
- Do NOT batch conversation-log writes across super-steps. Append per-message, synchronously, within the super-step boundary.
- Do NOT add conversation-log content to Langfuse traces. Tracing is the operator's channel; the log is the customer's.
- `sequence` is monotone but **NOT gapless** — Postgres IDENTITY column, consumers MUST page via `sequence > after_sequence`. Do NOT introduce `MAX+1` retry loops, advisory locks, or per-task sequences.
- Dedup is **idempotency-key-based**, not sequence-based. Every write constructs `idempotency_key = sha256(task_id || (checkpoint_id or "init") || origin_ref)` and uses `INSERT ... ON CONFLICT (task_id, idempotency_key) DO NOTHING`. Implementers MUST NOT swap this for a different dedup strategy.
- Rollback/redrive is **out of scope for v1**. Do NOT introduce `superseded_at`, `branch_id`, `parent_entry_id`, `mark_superseded`, or any other mutation on the log in v1. Claude Code's `parentUuid` DAG is the cautionary tale. When a rollback API ships in Phase 3+, that task will add the column + migration + `content_version=2` bump.
- `content` shape is **versioned**. Every entry has `content_version: SMALLINT NOT NULL DEFAULT 1`. New fields in an existing `kind` bump the version. Unknown versions are rendered as a debug fold by the Console, not silently ignored. `content_version=2` is RESERVED for Phase 3+ (rollback column + blob-offload).
- **Java owns the API read path; Python is append-only.** Do NOT add a `list_entries` method to the Python repository. Do NOT add any mutation methods to the Python repository. The Java repository reads Postgres directly — no cross-service RPC for reads.
- **Tenant isolation is mandatory on every read.** The Java query MUST filter by `(tenant_id, task_id)` with `tenant_id` resolved from the authenticated principal. The endpoint MUST NOT accept a client-supplied `tenant_id`. 404 (not 403) on wrong-tenant to prevent task-id enumeration.
- **Composite FK + CASCADE.** The `(task_id, tenant_id) REFERENCES tasks(task_id, tenant_id) ON DELETE CASCADE` constraint is load-bearing — it enforces tenant integrity on write and right-to-delete on tenant/task deletion. Do NOT weaken to a simple `REFERENCES tasks(task_id)`.
- **Rate-limit backstop.** No per-task row cap in v1. The log's growth is gated by Track 3's per-task token/USD budget — a loop that writes 10K log rows first hits the budget ceiling and dead-letters. Do NOT add a row-count cap in Task 13; defer to a future observability task if telemetry shows abuse.
- Retention: v1 accepts unbounded per-task log growth (Tier-0-capped entries only, so per-entry size is bounded). Task deletion cascades via ON DELETE CASCADE. Blob-storage offload, time-range partitioning, and customer export are Phase 3+ follow-ups.

## Assumptions

- Phase 1's `tasks` table with `tenant_id` column is live; multi-tenant isolation at the DB level matches every other task-scoped table.
- Postgres `jsonb` is the existing serialization format for structured payloads (matches `checkpoints.checkpoint_payload`, `task_events.details`).
- The Console's `TaskDetailPage` supports tabs (or can accept a new Conversation pane as a sibling section). If the page is single-scroll, this task adds a collapsible section at the top.
- Polling (5 s) is acceptable; a Phase 3+ upgrade path to SSE / WebSocket streaming is out of scope.

<!-- AGENT_TASK_END: task-13-user-facing-conversation-log.md -->
