-- Phase 2 Track 5: Agent Memory
-- Introduces the pgvector extension, the agent_memory_entries table (one row
-- per task_id, UPSERT on follow-up/redrive), the task_attached_memories join
-- table, and a per-task skip_memory_write override on tasks.
--
-- Runs after 0010_sandbox_support.sql. Requires the `pgvector/pgvector:pg16`
-- Docker image (or an RDS instance with the `vector` extension pre-installed).

-- Step 1: Enable pgvector extension (idempotent; safe to re-run)
CREATE EXTENSION IF NOT EXISTS vector;

-- Step 2: Immutable helper for joining a TEXT[] with a single-space delimiter.
-- Required because array_to_string(text[], text) is STABLE in Postgres, and a
-- STORED GENERATED column expression must be IMMUTABLE. We reconstruct the
-- behaviour using SELECT ... string_agg() over unnest(), which is composed
-- entirely of IMMUTABLE primitives (string_agg(text, text) is IMMUTABLE when
-- applied to TEXT values) and so can be marked IMMUTABLE safely. Used by the
-- content_tsv generated column below.
CREATE OR REPLACE FUNCTION immutable_array_to_string(arr TEXT[])
RETURNS TEXT
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
AS $$
    SELECT coalesce(string_agg(elem, ' '), '')
    FROM unnest(arr) AS elem;
$$;

-- Step 3: Create agent_memory_entries table
-- - task_id is a soft reference (no FK) so a task prune cannot cascade or
--   orphan memory rows.
-- - (tenant_id, agent_id) FK to agents IS enforced at the database level.
-- - summarizer_model_id is intentionally free-form (no FK) because it stores
--   template sentinels 'template:fallback' and 'template:dead_letter' in
--   addition to real model ids.
-- - content_vec is nullable to support the deferred-embedding fallback when
--   the embedding provider is down.
CREATE TABLE agent_memory_entries (
    memory_id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id              TEXT NOT NULL,
    agent_id               TEXT NOT NULL,
    task_id                UUID NOT NULL,
    title                  TEXT NOT NULL CHECK (length(title) <= 200),
    summary                TEXT NOT NULL,
    observations           TEXT[] NOT NULL DEFAULT '{}',
    outcome                TEXT NOT NULL CHECK (outcome IN ('succeeded', 'failed')),
    tags                   TEXT[] NOT NULL DEFAULT '{}',
    content_vec            vector(1536),
    content_tsv            tsvector GENERATED ALWAYS AS (
        to_tsvector(
            'english'::regconfig,
            coalesce(title, '') || ' ' ||
            coalesce(summary, '') || ' ' ||
            immutable_array_to_string(observations) || ' ' ||
            immutable_array_to_string(tags)
        )
    ) STORED,
    summarizer_model_id    TEXT,
    version                INT NOT NULL DEFAULT 1,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_agent_memory_entries_task_id UNIQUE (task_id),
    CONSTRAINT fk_agent_memory_entries_agent
        FOREIGN KEY (tenant_id, agent_id) REFERENCES agents (tenant_id, agent_id)
);

-- Step 4: Indexes on agent_memory_entries
-- - Btree composite supports list view scoped by (tenant_id, agent_id) ordered
--   by created_at DESC.
-- - GIN on content_tsv backs BM25-style full-text search.
-- - HNSW on content_vec with vector_cosine_ops backs vector similarity search;
--   pgvector defaults m=16, ef_construction=64.
CREATE INDEX idx_memory_entries_tenant_agent_created
    ON agent_memory_entries (tenant_id, agent_id, created_at DESC);

CREATE INDEX idx_memory_entries_tsv
    ON agent_memory_entries USING GIN (content_tsv);

CREATE INDEX idx_memory_entries_vec
    ON agent_memory_entries USING HNSW (content_vec vector_cosine_ops);

-- Step 5: Create task_attached_memories join table
-- - task_id has ON DELETE CASCADE so deleting a task drops its attachments.
-- - memory_id is a soft reference (no FK) so deleting a memory entry leaves
--   the attachment audit row in place.
CREATE TABLE task_attached_memories (
    task_id     UUID NOT NULL REFERENCES tasks (task_id) ON DELETE CASCADE,
    memory_id   UUID NOT NULL,
    position    INT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (task_id, memory_id)
);

-- Step 6: Reverse-lookup index on task_attached_memories.memory_id
CREATE INDEX idx_task_attached_memories_memory
    ON task_attached_memories (memory_id);

-- Step 7: Extend tasks table with skip_memory_write override
-- Per-task flag honoured by the worker memory write path (Task 6) and
-- persisted by the submission API (Task 4). Typed column (not JSONB key) so
-- the worker hot path avoids JSONB parse cost and the column is queryable
-- for operational reporting.
ALTER TABLE tasks ADD COLUMN skip_memory_write BOOLEAN NOT NULL DEFAULT FALSE;

-- Step 8: Table comments
COMMENT ON TABLE agent_memory_entries IS
    'Phase 2 Track 5: Durable, distilled per-task memory entries scoped to (tenant_id, agent_id). One row per task_id, UPSERT on follow-up/redrive.';
COMMENT ON TABLE task_attached_memories IS
    'Phase 2 Track 5: Task-to-memory attachments captured at submission time. task_id cascades on delete; memory_id is a soft reference so attachment audit survives memory deletion.';
COMMENT ON COLUMN tasks.skip_memory_write IS
    'Phase 2 Track 5: When true, the worker skips the memory write for this task even if agent_config.memory.enabled=true. Per-task privacy opt-out.';
