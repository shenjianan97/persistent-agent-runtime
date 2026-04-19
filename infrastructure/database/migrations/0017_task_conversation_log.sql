-- Phase 2 Track 7 Task 13: User-Facing Conversation Log
--
-- Separate append-only table that holds the user's view of an agent task,
-- independent of the LangGraph checkpointer (which persists the model's
-- compacted view). The Console "Conversation" pane and the API endpoint
-- `GET /v1/tasks/{id}/conversation` read from this table; compaction
-- Tiers 0/1/1.5 are invisible here, while Tier 3 is surfaced explicitly
-- via `compaction_boundary` entries.
--
-- Design contract: docs/exec-plans/active/phase-2/track-7/agent_tasks/
--   task-13-user-facing-conversation-log.md
--
-- Non-negotiable invariants enforced by this schema:
-- * Composite FK `(task_id, tenant_id) REFERENCES tasks(task_id, tenant_id)
--   ON DELETE CASCADE` — prevents tenant_id drift on write and cascades
--   right-to-delete through the existing task-delete path.
-- * Named CHECK constraint `chk_task_conversation_log_kind` — future
--   additions follow the Track 2 DROP+ADD pattern.
-- * `sequence BIGINT GENERATED ALWAYS AS IDENTITY` — monotone but NOT
--   gapless; consumers page via `sequence > N`.
-- * `UNIQUE(task_id, idempotency_key)` — full dedup across every row
--   (not partial). Append paths rely on `ON CONFLICT DO NOTHING`.
-- * Single index `(task_id, sequence)` covers the hot read path.
--
-- v1 is append-only. No `superseded_at`, no `branch_id`. `content_version=2`
-- is RESERVED for Phase 3+ (rollback / branching / blob-offload).

-- Step 1: Composite unique index on tasks prerequisite for the composite FK.
-- `task_id` is already a primary key; this materialises the `(task_id,
-- tenant_id)` tuple as a unique key so the FK below can reference it.
CREATE UNIQUE INDEX IF NOT EXISTS uq_tasks_task_tenant
    ON tasks (task_id, tenant_id);

-- Step 2: Create task_conversation_log table.
CREATE TABLE task_conversation_log (
    entry_id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id        TEXT NOT NULL,
    task_id          UUID NOT NULL,
    -- Monotone ordering — NOT gapless. Consumers page via `sequence > N`;
    -- gaps arise from rare insert retries / ON CONFLICT swallowed rows.
    sequence         BIGINT GENERATED ALWAYS AS IDENTITY,
    -- LangGraph checkpoint this entry was produced in. NULL only for
    -- `system_note` entries that are not tied to a specific super-step.
    checkpoint_id    TEXT,
    -- sha256(task_id || (checkpoint_id or "init") || origin_ref).
    -- ON CONFLICT DO NOTHING on (task_id, idempotency_key) makes every
    -- worker append idempotent across retries and crashes.
    idempotency_key  TEXT NOT NULL,
    kind             TEXT NOT NULL,
    role             TEXT,                  -- 'user' | 'assistant' | 'tool' | 'system'
    content_version  SMALLINT NOT NULL DEFAULT 1,
    content          JSONB NOT NULL,                -- shape per-kind (see spec §Content schema)
    content_size     INTEGER NOT NULL,              -- serialized bytes; Console "truncated" copy + ops dashboards
    metadata         JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
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
    CONSTRAINT uq_task_conversation_log_idem
        UNIQUE (task_id, idempotency_key)
);

-- Step 3: Hot-read index for `WHERE task_id = $1 AND sequence > $2
-- ORDER BY sequence LIMIT $3` (API pagination).
CREATE INDEX idx_task_conversation_log_task_seq
    ON task_conversation_log (task_id, sequence);

-- Step 4: Documentation.
COMMENT ON TABLE task_conversation_log IS
    'Phase 2 Track 7 Task 13: Append-only user-facing conversation log. '
    'Separate from the LangGraph checkpointer (which holds the compacted '
    'model view). Console reads this table via '
    'GET /v1/tasks/{id}/conversation. Best-effort; append failures log a '
    'WARN + increment conversation_log_append_failed_total but do not fail '
    'the task.';
COMMENT ON COLUMN task_conversation_log.sequence IS
    'Monotone but NOT gapless. Consumers MUST page via sequence > N.';
COMMENT ON COLUMN task_conversation_log.idempotency_key IS
    'sha256(task_id || (checkpoint_id or ''init'') || origin_ref). '
    'ON CONFLICT DO NOTHING on (task_id, idempotency_key).';
COMMENT ON COLUMN task_conversation_log.content_version IS
    'Schema version for the content JSON. v1 uses 1. '
    'content_version=2 is RESERVED for Phase 3+ (rollback / blob-offload).';
