import asyncio
import os
from typing import Any

import asyncpg

from core.config import WorkerConfig
from core.worker import WorkerService
from executor.router import DefaultTaskRouter

DEFAULT_TEST_CONFIG = {
    "heartbeat_interval_seconds": 2,
    "lease_duration_seconds": 10,
    "reaper_interval_seconds": 5,
    "reaper_jitter_seconds": 1,
    "max_concurrent_tasks": 10,
    "poll_backoff_initial_ms": 50,
    "poll_backoff_max_ms": 500,
    "shutdown_drain_seconds": 3,
}

_DEFAULT_DB_DSN = "postgresql://postgres:postgres@localhost:55432/persistent_agent_runtime"


async def create_worker(
    pool: asyncpg.Pool,
    *,
    worker_id: str | None = None,
    db_dsn: str | None = None,
    config_overrides: dict[str, Any] | None = None,
) -> WorkerService:
    cfg = dict(DEFAULT_TEST_CONFIG)
    if config_overrides:
        cfg.update(config_overrides)
    if worker_id:
        cfg["worker_id"] = worker_id

    resolved_db_dsn = db_dsn or os.getenv("E2E_DB_DSN", _DEFAULT_DB_DSN)
    config = WorkerConfig(db_dsn=resolved_db_dsn, **cfg)
    router = DefaultTaskRouter(config, pool)
    return WorkerService(config, pool, router)


async def stop_worker(worker: WorkerService) -> None:
    await worker.stop()
