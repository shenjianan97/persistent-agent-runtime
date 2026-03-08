"""Tests for HeartbeatManager — interval timing, lease revocation signaling."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.config import WorkerConfig
from core.heartbeat import HeartbeatHandle, HeartbeatManager
from core.logging import MetricsCollector


class TestHeartbeatHandle:
    async def test_handle_initial_state(self):
        event = asyncio.Event()
        task = asyncio.create_task(asyncio.sleep(100))
        handle = HeartbeatHandle("task-1", event, task)

        assert handle.task_id == "task-1"
        assert handle.lease_revoked is False
        assert not handle.cancel_event.is_set()

        await handle.stop()

    async def test_handle_stop_cancels_task(self):
        event = asyncio.Event()
        task = asyncio.create_task(asyncio.sleep(100))
        handle = HeartbeatHandle("task-1", event, task)

        await handle.stop()
        assert task.cancelled()


class TestHeartbeatManager:
    @pytest.fixture
    def mock_pool(self):
        pool = MagicMock()
        conn = AsyncMock()
        conn.execute = AsyncMock(return_value="UPDATE 1")
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=conn)
        ctx.__aexit__ = AsyncMock(return_value=False)
        pool.acquire = MagicMock(return_value=ctx)
        return pool

    @pytest.fixture
    def fast_config(self):
        return WorkerConfig(
            worker_id="test-worker",
            heartbeat_interval_seconds=0,  # No sleep for tests
        )

    async def test_start_heartbeat_creates_handle(self, mock_pool, fast_config):
        metrics = MetricsCollector()
        manager = HeartbeatManager(fast_config, mock_pool, metrics)

        handle = manager.start_heartbeat("task-123", "default")
        assert handle.task_id == "task-123"
        assert "task-123" in manager.active_tasks

        await handle.stop()

    async def test_stop_heartbeat_removes_handle(self, mock_pool, fast_config):
        metrics = MetricsCollector()
        manager = HeartbeatManager(fast_config, mock_pool, metrics)

        manager.start_heartbeat("task-123", "default")
        assert "task-123" in manager.active_tasks

        await manager.stop_heartbeat("task-123")
        # Give the task time to clean up
        await asyncio.sleep(0.01)
        assert "task-123" not in manager.active_tasks

    async def test_stop_all(self, mock_pool, fast_config):
        metrics = MetricsCollector()
        manager = HeartbeatManager(fast_config, mock_pool, metrics)

        manager.start_heartbeat("task-1", "default")
        manager.start_heartbeat("task-2", "default")
        assert len(manager.active_tasks) == 2

        await manager.stop_all()
        await asyncio.sleep(0.01)
        assert len(manager.active_tasks) == 0

    async def test_lease_revocation_on_zero_rows(self, fast_config):
        """When heartbeat UPDATE returns 0 rows, lease is revoked."""
        metrics = MetricsCollector()

        # Mock pool that returns "UPDATE 0"
        pool = MagicMock()
        conn = AsyncMock()
        conn.execute = AsyncMock(return_value="UPDATE 0")
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=conn)
        ctx.__aexit__ = AsyncMock(return_value=False)
        pool.acquire = MagicMock(return_value=ctx)

        revoked_tasks = []

        def on_revoked(task_id: str) -> None:
            revoked_tasks.append(task_id)

        # Use very short heartbeat interval for test speed
        config = WorkerConfig(
            worker_id="test-worker",
            heartbeat_interval_seconds=0,
        )

        manager = HeartbeatManager(config, pool, metrics, on_lease_revoked=on_revoked)
        handle = manager.start_heartbeat("task-revoked", "default")

        # Wait for the heartbeat to fire and detect revocation
        await asyncio.sleep(0.1)

        assert handle.lease_revoked is True
        assert handle.cancel_event.is_set()
        assert "task-revoked" in revoked_tasks
        assert metrics.get_counter("heartbeats.missed", worker_id="test-worker") >= 1

    async def test_heartbeat_sends_correct_query_params(self, fast_config):
        """Verify heartbeat sends task_id, tenant_id, worker_id."""
        metrics = MetricsCollector()
        captured_args = []

        pool = MagicMock()
        conn = AsyncMock()

        async def capture_execute(query, *args):
            captured_args.append(args)
            return "UPDATE 1"

        conn.execute = capture_execute
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=conn)
        ctx.__aexit__ = AsyncMock(return_value=False)
        pool.acquire = MagicMock(return_value=ctx)

        config = WorkerConfig(
            worker_id="test-worker-hb",
            heartbeat_interval_seconds=0,
        )

        manager = HeartbeatManager(config, pool, metrics)
        handle = manager.start_heartbeat("task-42", "tenant-x")

        await asyncio.sleep(0.05)
        await handle.stop()

        assert len(captured_args) >= 1
        # Args should be (task_id, tenant_id, worker_id)
        assert captured_args[0] == ("task-42", "tenant-x", "test-worker-hb")
