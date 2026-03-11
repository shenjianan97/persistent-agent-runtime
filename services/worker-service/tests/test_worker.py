"""Tests for WorkerService worker registry behavior."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from core.config import WorkerConfig
from core.worker import WorkerService


async def test_worker_heartbeat_restores_offline_worker_status():
    pool = MagicMock()
    conn = AsyncMock()
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool.acquire = MagicMock(return_value=ctx)

    config = WorkerConfig(
        worker_id="test-worker-registry",
        heartbeat_interval_seconds=1,
    )
    service = WorkerService(config, pool, router=MagicMock())

    sleep_calls = 0

    async def controlled_sleep(_: float) -> None:
        nonlocal sleep_calls
        sleep_calls += 1
        if sleep_calls > 1:
            raise asyncio.CancelledError

    with patch("core.worker.asyncio.sleep", new=controlled_sleep):
        await service._worker_heartbeat_loop()

    conn.execute.assert_awaited_once_with(
        "UPDATE workers SET status = 'online', last_heartbeat_at = NOW() WHERE worker_id = $1",
        "test-worker-registry",
    )
