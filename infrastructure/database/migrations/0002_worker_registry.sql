-- Worker registry table for tracking online workers
CREATE TABLE workers (
    worker_id             TEXT PRIMARY KEY,
    worker_pool_id        TEXT NOT NULL DEFAULT 'shared',
    tenant_id             TEXT NOT NULL DEFAULT 'default',
    status                TEXT NOT NULL DEFAULT 'online'
                          CHECK (status IN ('online', 'draining', 'offline')),
    last_heartbeat_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at            TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_workers_heartbeat ON workers (last_heartbeat_at)
    WHERE status = 'online';
