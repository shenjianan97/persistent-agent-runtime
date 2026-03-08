"""Tests for ReaperTask — jitter range, reaper scan logic."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.config import WorkerConfig
from core.logging import MetricsCollector
from core.reaper import ReaperTask


class TestReaperJitter:
    """Verify reaper interval jitter is within expected range."""

    def test_jitter_range_default(self):
        config = WorkerConfig(
            worker_id="test-reaper",
            reaper_interval_seconds=30,
            reaper_jitter_seconds=10,
        )
        metrics = MetricsCollector()
        pool = MagicMock()
        reaper = ReaperTask(config, pool, metrics)

        # Sample many intervals and verify bounds
        intervals = [reaper._jittered_interval() for _ in range(1000)]

        assert min(intervals) >= 20.0  # 30 - 10
        assert max(intervals) <= 40.0  # 30 + 10
        # Verify there's actual variation (not constant)
        assert max(intervals) - min(intervals) > 5.0

    def test_jitter_range_custom(self):
        config = WorkerConfig(
            worker_id="test-reaper",
            reaper_interval_seconds=60,
            reaper_jitter_seconds=5,
        )
        metrics = MetricsCollector()
        pool = MagicMock()
        reaper = ReaperTask(config, pool, metrics)

        intervals = [reaper._jittered_interval() for _ in range(1000)]

        assert min(intervals) >= 55.0
        assert max(intervals) <= 65.0

    def test_zero_jitter(self):
        config = WorkerConfig(
            worker_id="test-reaper",
            reaper_interval_seconds=30,
            reaper_jitter_seconds=0,
        )
        metrics = MetricsCollector()
        pool = MagicMock()
        reaper = ReaperTask(config, pool, metrics)

        intervals = [reaper._jittered_interval() for _ in range(100)]
        assert all(i == 30.0 for i in intervals)


class TestReaperRunOnce:
    """Test the reaper's run_once scan logic with mocked database responses."""

    @pytest.fixture
    def mock_pool(self):
        pool = MagicMock()
        conn = AsyncMock()
        conn.fetch = AsyncMock(return_value=[])
        conn.fetchrow = AsyncMock(return_value={"depth": 0})
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=conn)
        ctx.__aexit__ = AsyncMock(return_value=False)
        pool.acquire = MagicMock(return_value=ctx)
        return pool, conn

    async def test_run_once_empty(self, mock_pool):
        pool, conn = mock_pool
        config = WorkerConfig(worker_id="test-reaper")
        metrics = MetricsCollector()
        reaper = ReaperTask(config, pool, metrics)

        results = await reaper.run_once()

        assert results["requeued"] == []
        assert results["dead_lettered_expired"] == []
        assert results["dead_lettered_timeout"] == []
        assert metrics.get_gauge("queue.depth") == 0

    async def test_run_once_with_requeued(self, mock_pool):
        pool, conn = mock_pool
        import uuid

        task_id = uuid.uuid4()

        # First fetch returns requeued tasks, rest return empty
        call_count = 0

        async def mock_fetch(query, *args):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return [{"task_id": task_id}]
            return []

        conn.fetch = mock_fetch

        config = WorkerConfig(worker_id="test-reaper")
        metrics = MetricsCollector()
        reaper = ReaperTask(config, pool, metrics)

        results = await reaper.run_once()

        assert len(results["requeued"]) == 1
        assert results["requeued"][0] == str(task_id)
        assert metrics.get_counter("leases.expired") >= 1

    async def test_run_once_with_dead_lettered(self, mock_pool):
        pool, conn = mock_pool
        import uuid

        task_id = uuid.uuid4()

        call_count = 0

        async def mock_fetch(query, *args):
            nonlocal call_count
            call_count += 1
            if call_count == 2:  # Second query is dead-letter
                return [{"task_id": task_id}]
            return []

        conn.fetch = mock_fetch

        config = WorkerConfig(worker_id="test-reaper")
        metrics = MetricsCollector()
        reaper = ReaperTask(config, pool, metrics)

        results = await reaper.run_once()

        assert len(results["dead_lettered_expired"]) == 1
        assert metrics.get_counter("tasks.dead_letter") >= 1

    async def test_run_once_with_timeout(self, mock_pool):
        pool, conn = mock_pool
        import uuid

        task_id = uuid.uuid4()

        call_count = 0

        async def mock_fetch(query, *args):
            nonlocal call_count
            call_count += 1
            if call_count == 3:  # Third query is timeout
                return [{"task_id": task_id}]
            return []

        conn.fetch = mock_fetch

        config = WorkerConfig(worker_id="test-reaper")
        metrics = MetricsCollector()
        reaper = ReaperTask(config, pool, metrics)

        results = await reaper.run_once()

        assert len(results["dead_lettered_timeout"]) == 1
        assert metrics.get_counter("tasks.dead_letter") >= 1

    async def test_run_once_updates_queue_depth(self, mock_pool):
        pool, conn = mock_pool
        conn.fetchrow = AsyncMock(return_value={"depth": 42})

        config = WorkerConfig(worker_id="test-reaper")
        metrics = MetricsCollector()
        reaper = ReaperTask(config, pool, metrics)

        await reaper.run_once()

        assert metrics.get_gauge("queue.depth") == 42


class TestReaperLifecycle:
    async def test_start_stop(self):
        pool = MagicMock()
        config = WorkerConfig(worker_id="test-reaper")
        metrics = MetricsCollector()
        reaper = ReaperTask(config, pool, metrics)

        await reaper.start()
        assert reaper.running is True

        await reaper.stop()
        assert reaper.running is False
