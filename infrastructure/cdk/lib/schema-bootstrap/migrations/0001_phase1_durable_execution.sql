CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- Tasks table (also serves as the queue in Phase 1)
CREATE TABLE tasks (
    task_id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id             TEXT NOT NULL DEFAULT 'default',
    agent_id              TEXT NOT NULL,
    agent_config_snapshot JSONB NOT NULL,
    status                TEXT NOT NULL DEFAULT 'queued'
                          CHECK (status IN ('queued', 'running', 'completed', 'dead_letter')),
    worker_pool_id        TEXT NOT NULL DEFAULT 'shared',
    version               INT NOT NULL DEFAULT 1,
    input                 TEXT NOT NULL,
    output                JSONB,
    lease_owner           TEXT,
    lease_expiry          TIMESTAMPTZ,
    retry_count           INT NOT NULL DEFAULT 0,
    max_retries           INT NOT NULL DEFAULT 3,
    retry_after           TIMESTAMPTZ,
    retry_history         JSONB NOT NULL DEFAULT '[]'::jsonb,
    task_timeout_seconds  INT NOT NULL DEFAULT 3600,
    max_steps             INT NOT NULL DEFAULT 100,
    last_error_code       TEXT,
    last_error_message    TEXT,
    last_worker_id        TEXT,
    dead_letter_reason    TEXT
                          CHECK (
                              dead_letter_reason IS NULL OR dead_letter_reason IN (
                                  'cancelled_by_user',
                                  'retries_exhausted',
                                  'task_timeout',
                                  'non_retryable_error',
                                  'max_steps_exceeded'
                              )
                          ),
    dead_lettered_at      TIMESTAMPTZ,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_tasks_claim ON tasks (worker_pool_id, created_at)
    WHERE status = 'queued';

CREATE INDEX idx_tasks_lease_expiry ON tasks (lease_expiry)
    WHERE status = 'running' AND lease_expiry IS NOT NULL;

CREATE INDEX idx_tasks_timeout ON tasks (created_at)
    WHERE status IN ('running', 'queued');

CREATE INDEX idx_tasks_tenant_agent ON tasks (tenant_id, agent_id, created_at);

CREATE INDEX idx_tasks_dead_letter ON tasks (tenant_id, agent_id, dead_lettered_at DESC, task_id DESC)
    WHERE status = 'dead_letter';

-- Checkpoints table (acts as LangGraph BaseCheckpointSaver)
CREATE TABLE checkpoints (
    task_id               UUID NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
    checkpoint_ns         TEXT NOT NULL DEFAULT '',
    checkpoint_id         TEXT NOT NULL,
    worker_id             TEXT NOT NULL,
    parent_checkpoint_id  TEXT,
    thread_ts             TEXT NOT NULL,
    parent_ts             TEXT,
    checkpoint_payload    JSONB NOT NULL,
    metadata_payload      JSONB NOT NULL DEFAULT '{}'::jsonb,
    cost_microdollars     INT NOT NULL DEFAULT 0,
    execution_metadata    JSONB,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    PRIMARY KEY (task_id, checkpoint_ns, checkpoint_id)
);

CREATE INDEX idx_checkpoints_task_ts ON checkpoints (task_id, thread_ts);
CREATE INDEX idx_checkpoints_task_created ON checkpoints (task_id, checkpoint_ns, created_at);

-- Checkpoint writes table (stores pending writes within a super-step)
CREATE TABLE checkpoint_writes (
    task_id               UUID NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
    checkpoint_ns         TEXT NOT NULL DEFAULT '',
    checkpoint_id         TEXT NOT NULL,
    writer_task_id        TEXT,
    task_path             TEXT NOT NULL DEFAULT '',
    idx                   INT NOT NULL,
    channel               TEXT NOT NULL,
    type                  TEXT,
    blob                  BYTEA NOT NULL,

    PRIMARY KEY (task_id, checkpoint_ns, checkpoint_id, task_path, idx)
    -- No FK to checkpoints: LangGraph calls aput_writes() before aput(),
    -- so the checkpoint row does not exist yet when writes are inserted.
);
