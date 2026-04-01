\set ON_ERROR_STOP 1

BEGIN;

DO $$
DECLARE
    expected_tables TEXT[] := ARRAY['tasks', 'checkpoints', 'checkpoint_writes', 'agents'];
    expected_indexes TEXT[] := ARRAY[
        'idx_tasks_claim',
        'idx_tasks_lease_expiry',
        'idx_tasks_timeout',
        'idx_tasks_tenant_agent',
        'idx_tasks_dead_letter',
        'idx_checkpoints_task_ts',
        'idx_checkpoints_task_created',
        'idx_agents_tenant_status'
    ];
    item TEXT;
BEGIN
    FOREACH item IN ARRAY expected_tables LOOP
        IF NOT EXISTS (
            SELECT 1
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'public'
              AND c.relkind = 'r'
              AND c.relname = item
        ) THEN
            RAISE EXCEPTION 'missing table: %', item;
        END IF;
    END LOOP;

    FOREACH item IN ARRAY expected_indexes LOOP
        IF NOT EXISTS (
            SELECT 1
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'public'
              AND c.relkind = 'i'
              AND c.relname = item
        ) THEN
            RAISE EXCEPTION 'missing index: %', item;
        END IF;
    END LOOP;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'tasks_status_check'
    ) THEN
        RAISE EXCEPTION 'missing tasks_status_check';
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'tasks_dead_letter_reason_check'
    ) THEN
        RAISE EXCEPTION 'missing tasks_dead_letter_reason_check';
    END IF;
END $$;

-- Insert test agent so FK constraint on tasks is satisfied
INSERT INTO agents (tenant_id, agent_id, display_name, agent_config, status)
VALUES ('default', 'agent-1', 'Verification Agent',
        '{"system_prompt":"test","provider":"anthropic","model":"claude","temperature":0.5,"allowed_tools":["web_search"]}'::jsonb,
        'active');

INSERT INTO tasks (task_id, tenant_id, agent_id, agent_config_snapshot, input)
VALUES
    ('00000000-0000-0000-0000-000000000001', 'default', 'agent-1', '{"model":"claude","allowed_tools":["web_search"]}', 'claim-first'),
    ('00000000-0000-0000-0000-000000000002', 'default', 'agent-1', '{"model":"claude","allowed_tools":["web_search"]}', 'claim-second');

WITH claimable AS (
    SELECT task_id
    FROM tasks
    WHERE status = 'queued'
      AND worker_pool_id = 'shared'
      AND tenant_id = 'default'
      AND (retry_after IS NULL OR retry_after < NOW())
    ORDER BY created_at, task_id
    LIMIT 1
    FOR UPDATE SKIP LOCKED
)
UPDATE tasks t
SET status = 'running',
    lease_owner = 'worker-1',
    lease_expiry = NOW() + INTERVAL '60 seconds',
    version = t.version + 1,
    updated_at = NOW()
FROM claimable c
WHERE t.task_id = c.task_id;

DO $$
BEGIN
    IF (SELECT status FROM tasks WHERE task_id = '00000000-0000-0000-0000-000000000001') <> 'running' THEN
        RAISE EXCEPTION 'claim query did not move first task to running';
    END IF;

    IF (SELECT lease_owner FROM tasks WHERE task_id = '00000000-0000-0000-0000-000000000001') <> 'worker-1' THEN
        RAISE EXCEPTION 'claim query did not set lease_owner';
    END IF;

    IF (SELECT status FROM tasks WHERE task_id = '00000000-0000-0000-0000-000000000002') <> 'queued' THEN
        RAISE EXCEPTION 'claim query unexpectedly modified second task';
    END IF;
END $$;

UPDATE tasks
SET lease_expiry = NOW() + INTERVAL '60 seconds',
    updated_at = NOW()
WHERE task_id = '00000000-0000-0000-0000-000000000001'
  AND tenant_id = 'default'
  AND lease_owner = 'worker-1'
  AND status = 'running';

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM tasks
        WHERE task_id = '00000000-0000-0000-0000-000000000001'
          AND lease_expiry > NOW()
    ) THEN
        RAISE EXCEPTION 'heartbeat did not extend lease';
    END IF;
END $$;

INSERT INTO checkpoints (
    task_id,
    checkpoint_ns,
    checkpoint_id,
    worker_id,
    parent_checkpoint_id,
    thread_ts,
    parent_ts,
    checkpoint_payload,
    metadata_payload
)
SELECT
    '00000000-0000-0000-0000-000000000001',
    '',
    'cp-1',
    'worker-1',
    NULL,
    '2026-03-05T10:00:01.123456+00:00',
    NULL,
    '{"channel_values":{"messages":[]}}',
    '{"source":"worker"}'
FROM tasks t
WHERE t.task_id = '00000000-0000-0000-0000-000000000001'
  AND t.tenant_id = 'default'
  AND t.status = 'running'
  AND t.lease_owner = 'worker-1'
ON CONFLICT (task_id, checkpoint_ns, checkpoint_id) DO UPDATE
SET checkpoint_payload = EXCLUDED.checkpoint_payload,
    metadata_payload = EXCLUDED.metadata_payload,
    worker_id = EXCLUDED.worker_id,
    parent_checkpoint_id = EXCLUDED.parent_checkpoint_id,
    thread_ts = EXCLUDED.thread_ts,
    parent_ts = EXCLUDED.parent_ts;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM checkpoints
        WHERE task_id = '00000000-0000-0000-0000-000000000001'
          AND checkpoint_ns = ''
          AND checkpoint_id = 'cp-1'
          AND worker_id = 'worker-1'
    ) THEN
        RAISE EXCEPTION 'lease-aware checkpoint insert failed';
    END IF;
END $$;

INSERT INTO checkpoint_writes (task_id, checkpoint_ns, checkpoint_id, writer_task_id, task_path, idx, channel, type, blob)
VALUES ('00000000-0000-0000-0000-000000000001', '', 'cp-1', 'writer-1', '', 0, 'messages', 'json', decode('7b7d', 'hex'))
ON CONFLICT (task_id, checkpoint_ns, checkpoint_id, task_path, idx)
DO UPDATE SET channel = EXCLUDED.channel, type = EXCLUDED.type, blob = EXCLUDED.blob;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM checkpoint_writes
        WHERE task_id = '00000000-0000-0000-0000-000000000001'
          AND checkpoint_id = 'cp-1'
          AND idx = 0
          AND writer_task_id = 'writer-1'
    ) THEN
        RAISE EXCEPTION 'checkpoint_writes insert failed';
    END IF;
END $$;

UPDATE checkpoints
SET cost_microdollars = 123,
    execution_metadata = '{"latency_ms":42}'
WHERE task_id = '00000000-0000-0000-0000-000000000001'
  AND checkpoint_ns = ''
  AND checkpoint_id = 'cp-1';

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM checkpoints
        WHERE task_id = '00000000-0000-0000-0000-000000000001'
          AND checkpoint_id = 'cp-1'
          AND cost_microdollars = 123
    ) THEN
        RAISE EXCEPTION 'checkpoint cost update failed';
    END IF;
END $$;

INSERT INTO tasks (
    task_id,
    tenant_id,
    agent_id,
    agent_config_snapshot,
    input,
    status,
    lease_owner,
    lease_expiry,
    retry_count,
    max_retries
)
VALUES (
    '00000000-0000-0000-0000-000000000003',
    'default',
    'agent-1',
    '{"model":"claude","allowed_tools":["web_search"]}',
    'retry-me',
    'running',
    'worker-2',
    NOW() + INTERVAL '60 seconds',
    0,
    3
);

WITH requeued AS (
    UPDATE tasks
    SET status = 'queued',
        lease_owner = NULL,
        lease_expiry = NULL,
        retry_count = retry_count + 1,
        retry_after = NOW() + (POWER(2, retry_count) * INTERVAL '1 second'),
        retry_history = retry_history || jsonb_build_array(NOW()),
        last_error_code = 'transient_error',
        last_error_message = 'retryable failure',
        version = version + 1,
        updated_at = NOW()
    WHERE task_id = '00000000-0000-0000-0000-000000000003'
      AND tenant_id = 'default'
      AND status = 'running'
      AND lease_owner = 'worker-2'
      AND retry_count < max_retries
    RETURNING task_id, worker_pool_id
)
, notified AS (
    SELECT pg_notify('new_task', worker_pool_id)
    FROM requeued
)
SELECT 1;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM tasks
        WHERE task_id = '00000000-0000-0000-0000-000000000003'
          AND status = 'queued'
          AND retry_count = 1
          AND lease_owner IS NULL
          AND lease_expiry IS NULL
          AND jsonb_array_length(retry_history) = 1
          AND retry_after IS NOT NULL
    ) THEN
        RAISE EXCEPTION 'retry requeue query failed';
    END IF;
END $$;

INSERT INTO tasks (
    task_id,
    tenant_id,
    agent_id,
    agent_config_snapshot,
    input,
    status,
    lease_owner,
    lease_expiry,
    retry_count,
    max_retries
)
VALUES (
    '00000000-0000-0000-0000-000000000004',
    'default',
    'agent-1',
    '{"model":"claude","allowed_tools":["web_search"]}',
    'reap-me',
    'running',
    'worker-3',
    NOW() - INTERVAL '5 seconds',
    0,
    3
);

WITH requeued AS (
    UPDATE tasks
    SET status = 'queued',
        lease_owner = NULL,
        lease_expiry = NULL,
        retry_count = retry_count + 1,
        retry_after = NOW() + (POWER(2, retry_count) * INTERVAL '1 second'),
        retry_history = retry_history || jsonb_build_array(NOW()),
        version = version + 1,
        updated_at = NOW()
    WHERE status = 'running'
      AND lease_expiry < NOW()
      AND retry_count < max_retries
      AND task_id = '00000000-0000-0000-0000-000000000004'
    RETURNING task_id, worker_pool_id
)
, notified AS (
    SELECT pg_notify('new_task', worker_pool_id)
    FROM requeued
)
SELECT 1;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM tasks
        WHERE task_id = '00000000-0000-0000-0000-000000000004'
          AND status = 'queued'
          AND retry_count = 1
          AND lease_owner IS NULL
          AND retry_after IS NOT NULL
    ) THEN
        RAISE EXCEPTION 'reaper reclaim query failed';
    END IF;
END $$;

INSERT INTO tasks (
    task_id,
    tenant_id,
    agent_id,
    agent_config_snapshot,
    input,
    status,
    created_at,
    task_timeout_seconds
)
VALUES (
    '00000000-0000-0000-0000-000000000005',
    'default',
    'agent-1',
    '{"model":"claude","allowed_tools":["web_search"]}',
    'timeout-me',
    'queued',
    NOW() - INTERVAL '2 hours',
    60
);

UPDATE tasks
SET status = 'dead_letter',
    last_worker_id = lease_owner,
    lease_owner = NULL,
    lease_expiry = NULL,
    last_error_code = 'task_timeout',
    last_error_message = 'task exceeded task_timeout_seconds',
    dead_letter_reason = 'task_timeout',
    dead_lettered_at = NOW(),
    version = version + 1,
    updated_at = NOW()
WHERE status IN ('running', 'queued')
  AND created_at + (task_timeout_seconds * INTERVAL '1 second') < NOW()
  AND task_id = '00000000-0000-0000-0000-000000000005';

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM tasks
        WHERE task_id = '00000000-0000-0000-0000-000000000005'
          AND status = 'dead_letter'
          AND dead_letter_reason = 'task_timeout'
    ) THEN
        RAISE EXCEPTION 'timeout dead-letter query failed';
    END IF;
END $$;

INSERT INTO tasks (task_id, tenant_id, agent_id, agent_config_snapshot, input)
VALUES (
    '00000000-0000-0000-0000-000000000006',
    'default',
    'agent-1',
    '{"model":"claude","allowed_tools":["web_search"]}',
    'cancel-me'
);

UPDATE tasks
SET status = 'dead_letter',
    last_worker_id = lease_owner,
    lease_owner = NULL,
    lease_expiry = NULL,
    last_error_code = 'cancelled_by_user',
    last_error_message = 'task cancelled by user request',
    dead_letter_reason = 'cancelled_by_user',
    dead_lettered_at = NOW(),
    version = version + 1,
    updated_at = NOW()
WHERE task_id = '00000000-0000-0000-0000-000000000006'
  AND tenant_id = 'default'
  AND status IN ('queued', 'running');

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM tasks
        WHERE task_id = '00000000-0000-0000-0000-000000000006'
          AND status = 'dead_letter'
          AND dead_letter_reason = 'cancelled_by_user'
    ) THEN
        RAISE EXCEPTION 'cancel query failed';
    END IF;
END $$;

UPDATE tasks
SET status = 'dead_letter',
    last_worker_id = 'worker-1',
    lease_owner = NULL,
    lease_expiry = NULL,
    last_error_code = 'non_retryable_error',
    last_error_message = 'fatal',
    dead_letter_reason = 'non_retryable_error',
    dead_lettered_at = NOW(),
    version = version + 1,
    updated_at = NOW()
WHERE task_id = '00000000-0000-0000-0000-000000000001'
  AND tenant_id = 'default'
  AND status = 'running'
  AND lease_owner = 'worker-1';

WITH redriven AS (
    UPDATE tasks
    SET status = 'queued',
        retry_count = 0,
        retry_after = NULL,
        lease_owner = NULL,
        lease_expiry = NULL,
        last_error_code = NULL,
        last_error_message = NULL,
        last_worker_id = NULL,
        dead_letter_reason = NULL,
        dead_lettered_at = NULL,
        version = version + 1,
        updated_at = NOW()
    WHERE task_id = '00000000-0000-0000-0000-000000000001'
      AND tenant_id = 'default'
      AND status = 'dead_letter'
    RETURNING task_id, worker_pool_id
)
, notified AS (
    SELECT pg_notify('new_task', worker_pool_id)
    FROM redriven
)
SELECT 1;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM tasks
        WHERE task_id = '00000000-0000-0000-0000-000000000001'
          AND status = 'queued'
          AND retry_count = 0
          AND retry_after IS NULL
          AND dead_letter_reason IS NULL
          AND last_error_code IS NULL
    ) THEN
        RAISE EXCEPTION 'redrive query failed';
    END IF;
END $$;

DO $$
BEGIN
    BEGIN
        INSERT INTO tasks (task_id, tenant_id, agent_id, agent_config_snapshot, input, status)
        VALUES (
            '00000000-0000-0000-0000-000000000007',
            'default',
            'agent-1',
            '{"model":"claude"}',
            'bad-status',
            'not_valid'
        );
        RAISE EXCEPTION 'invalid status insert unexpectedly succeeded';
    EXCEPTION
        WHEN check_violation THEN NULL;
    END;

    BEGIN
        INSERT INTO tasks (task_id, tenant_id, agent_id, agent_config_snapshot, input, dead_letter_reason)
        VALUES (
            '00000000-0000-0000-0000-000000000008',
            'default',
            'agent-1',
            '{"model":"claude"}',
            'bad-dead-letter',
            'wrong_reason'
        );
        RAISE EXCEPTION 'invalid dead_letter_reason insert unexpectedly succeeded';
    EXCEPTION
        WHEN check_violation THEN NULL;
    END;

    BEGIN
        INSERT INTO tasks (task_id, tenant_id, agent_id, input)
        VALUES (
            '00000000-0000-0000-0000-000000000009',
            'default',
            'agent-1',
            'missing-config'
        );
        RAISE EXCEPTION 'malformed insert unexpectedly succeeded';
    EXCEPTION
        WHEN not_null_violation THEN NULL;
    END;

END $$;

-- Verify FK enforcement: task insert with unknown agent_id must fail
DO $$
BEGIN
    BEGIN
        INSERT INTO tasks (task_id, tenant_id, agent_id, agent_config_snapshot, input)
        VALUES (
            '00000000-0000-0000-0000-000000000010',
            'default',
            'nonexistent-agent',
            '{"model":"claude"}',
            'fk-violation-test'
        );
        RAISE EXCEPTION 'FK violation insert unexpectedly succeeded';
    EXCEPTION
        WHEN foreign_key_violation THEN NULL;
    END;
END $$;

-- Verify agents CHECK constraints
DO $$
BEGIN
    -- Invalid status value
    BEGIN
        INSERT INTO agents (tenant_id, agent_id, display_name, agent_config, status)
        VALUES ('default', 'bad-status-agent', 'Bad', '{"model":"test"}'::jsonb, 'paused');
        RAISE EXCEPTION 'invalid agent status insert unexpectedly succeeded';
    EXCEPTION
        WHEN check_violation THEN NULL;
    END;

    -- display_name exceeding 200 chars
    BEGIN
        INSERT INTO agents (tenant_id, agent_id, display_name, agent_config, status)
        VALUES ('default', 'long-name-agent', repeat('x', 201), '{"model":"test"}'::jsonb, 'active');
        RAISE EXCEPTION 'oversized display_name insert unexpectedly succeeded';
    EXCEPTION
        WHEN check_violation THEN NULL;
    END;

    -- agent_id exceeding 64 chars
    BEGIN
        INSERT INTO agents (tenant_id, agent_id, display_name, agent_config, status)
        VALUES ('default', repeat('a', 65), 'Long ID Agent', '{"model":"test"}'::jsonb, 'active');
        RAISE EXCEPTION 'oversized agent_id insert unexpectedly succeeded';
    EXCEPTION
        WHEN check_violation THEN NULL;
    END;
END $$;

ROLLBACK;
