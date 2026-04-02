import asyncio
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


async def create_worker(
    pool: asyncpg.Pool,
    *,
    worker_id: str | None = None,
    db_dsn: str = "postgresql://postgres:postgres@localhost:55432/persistent_agent_runtime",
    config_overrides: dict[str, Any] | None = None,
) -> WorkerService:
    cfg = dict(DEFAULT_TEST_CONFIG)
    if config_overrides:
        cfg.update(config_overrides)
    if worker_id:
        cfg["worker_id"] = worker_id

    config = WorkerConfig(db_dsn=db_dsn, **cfg)
    router = DefaultTaskRouter(config, pool)
    return WorkerService(config, pool, router)


async def stop_worker(worker: WorkerService) -> None:
    # Snapshot tasks before stop so we can cancel orphaned coroutines after.
    tasks_before = {t for t in asyncio.all_tasks() if not t.done()}
    await worker.stop()
    # Cancel any tasks that were spawned during the worker's lifetime and
    # survived the graceful drain (e.g. asyncio.sleep in mock LLMs).
    tasks_after = asyncio.all_tasks()
    orphans = {t for t in tasks_after if not t.done()} - tasks_before
    for t in orphans:
        t.cancel()
    if orphans:
        await asyncio.gather(*orphans, return_exceptions=True)
