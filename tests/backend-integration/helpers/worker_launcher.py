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

_DB_DSN = "postgresql://postgres:postgres@localhost:55432/persistent_agent_runtime"


async def create_worker(
    pool: asyncpg.Pool,
    *,
    worker_id: str | None = None,
    db_dsn: str = _DB_DSN,
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
    # Terminate any DB connections stuck in "idle in transaction" state.
    # These hold row locks from cancelled coroutines and block test cleanup.
    # Use a fresh direct connection to avoid deadlocking on the shared pool.
    try:
        conn = await asyncpg.connect(worker._config.db_dsn)  # noqa: SLF001
        try:
            await conn.execute("""
                SELECT pg_terminate_backend(pid)
                FROM pg_stat_activity
                WHERE datname = current_database()
                  AND state = 'idle in transaction'
                  AND pid <> pg_backend_pid()
            """)
        finally:
            await conn.close()
    except Exception:
        pass
    # Give terminated connections a moment to release locks
    await asyncio.sleep(0.2)
