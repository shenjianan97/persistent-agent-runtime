-- =============================================================================
-- Migration 0009: Artifact Storage
-- =============================================================================
-- Track: Agent Capabilities — Track 1 (Output Artifact Storage)
-- Adds the task_artifacts table for storing metadata about files produced by
-- agents (output) or attached by users (input). Actual file content is stored
-- in S3; this table holds only the metadata and S3 key reference.
-- =============================================================================

-- Step 1: Create task_artifacts table for artifact metadata
CREATE TABLE task_artifacts (
    artifact_id   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    task_id       UUID NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
    tenant_id     TEXT NOT NULL,
    filename      TEXT NOT NULL,
    direction     TEXT NOT NULL CHECK (direction IN ('input', 'output')),
    content_type  TEXT NOT NULL,
    size_bytes    BIGINT NOT NULL,
    s3_key        TEXT NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Step 2: Unique constraint — prevent duplicate artifacts per task/direction/filename
ALTER TABLE task_artifacts ADD CONSTRAINT uq_task_artifacts_task_direction_filename
    UNIQUE (task_id, direction, filename);

-- Step 3: Composite index for efficient tenant-scoped artifact listing
CREATE INDEX idx_task_artifacts_tenant_task ON task_artifacts (tenant_id, task_id);

-- Step 4: Table comment
COMMENT ON TABLE task_artifacts IS 'Metadata for files produced by agents (output) or attached by users (input). Actual file content stored in S3.';
