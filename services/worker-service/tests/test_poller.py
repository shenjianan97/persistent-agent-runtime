"""Tests for TaskPoller — claim query, LISTEN/NOTIFY, backoff behavior."""

import asyncio
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.config import WorkerConfig
from core.logging import MetricsCollector
from core.poller import build_claim_query, TaskPoller


class TestClaimQuery:
    """Verify the claim query matches the design doc."""

    def test_claim_query_has_for_update_skip_locked(self):
        assert "FOR UPDATE SKIP LOCKED" in build_claim_query(60)

    def test_claim_query_checks_retry_after(self):
        assert "retry_after IS NULL OR retry_after < NOW()" in build_claim_query(60)

    def test_claim_query_uses_cte(self):
        assert "WITH claimable AS" in build_claim_query(60)

    def test_claim_query_sets_running(self):
        assert "status = 'running'" in build_claim_query(60)

    def test_claim_query_sets_lease_expiry(self):
        assert "lease_expiry = NOW() + INTERVAL '60 seconds'" in build_claim_query(60)

    def test_claim_query_returns_full_row(self):
        assert "RETURNING t.*" in build_claim_query(60)

    def test_claim_query_orders_by_created_at(self):
        assert "ORDER BY created_at" in build_claim_query(60)

    def test_claim_query_filters_by_pool_and_tenant(self):
        assert "worker_pool_id = $1" in build_claim_query(60)
        assert "tenant_id = $2" in build_claim_query(60)

    def test_claim_query_increments_version(self):
        assert "version = t.version + 1" in build_claim_query(60)


class TestPollerBackoff:
    """Test that the poller applies correct backoff on empty polls."""

    def _make_poller(self, config=None) -> TaskPoller:
        config = config or WorkerConfig(worker_id="test-poller")
        pool = MagicMock()
        metrics = MetricsCollector()
        return TaskPoller(config, pool, metrics, MagicMock(), MagicMock())

    def test_initial_backoff(self):
        poller = self._make_poller()
        assert poller._backoff_ms == 100

    def test_backoff_doubles_on_empty(self):
        poller = self._make_poller()
        # Simulate empty poll progression
        for expected in [200, 400, 800, 1600, 3200, 5000, 5000]:
            poller._backoff_ms = min(
                int(poller._backoff_ms * poller._config.poll_backoff_multiplier),
                poller._config.poll_backoff_max_ms,
            )
            assert poller._backoff_ms == expected

    def test_backoff_resets(self):
        poller = self._make_poller()
        poller._backoff_ms = 3200
        poller.reset_backoff()
        assert poller._backoff_ms == 100


class TestPollerNotify:
    """Test LISTEN/NOTIFY integration."""

    def test_on_notify_sets_event_for_matching_pool(self):
        config = WorkerConfig(worker_id="test-poller", worker_pool_id="shared")
        pool = MagicMock()
        metrics = MetricsCollector()
        poller = TaskPoller(config, pool, metrics, MagicMock(), MagicMock())

        poller._on_notify(MagicMock(), 0, "new_task", "shared")
        assert poller._notify_event.is_set()

    def test_on_notify_ignores_other_pool(self):
        config = WorkerConfig(worker_id="test-poller", worker_pool_id="shared")
        pool = MagicMock()
        metrics = MetricsCollector()
        poller = TaskPoller(config, pool, metrics, MagicMock(), MagicMock())

        poller._on_notify(MagicMock(), 0, "new_task", "other_pool")
        assert not poller._notify_event.is_set()

    def test_on_notify_accepts_empty_payload(self):
        config = WorkerConfig(worker_id="test-poller", worker_pool_id="shared")
        pool = MagicMock()
        metrics = MetricsCollector()
        poller = TaskPoller(config, pool, metrics, MagicMock(), MagicMock())

        poller._on_notify(MagicMock(), 0, "new_task", "")
        assert poller._notify_event.is_set()


class TestPollerSemaphore:
    def test_semaphore_exposed(self):
        config = WorkerConfig(worker_id="test-poller", max_concurrent_tasks=7)
        pool = MagicMock()
        metrics = MetricsCollector()
        poller = TaskPoller(config, pool, metrics, MagicMock(), MagicMock())

        assert isinstance(poller.semaphore, asyncio.Semaphore)


class TestPollerTryClaim:
    """Test the _try_claim method with mocked database."""

    async def test_try_claim_returns_false_when_no_task(self):
        config = WorkerConfig(worker_id="test-poller")
        pool = MagicMock()
        conn = AsyncMock()
        conn.fetchrow = AsyncMock(return_value=None)
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=conn)
        ctx.__aexit__ = AsyncMock(return_value=False)
        pool.acquire = MagicMock(return_value=ctx)
        metrics = MetricsCollector()
        poller = TaskPoller(config, pool, metrics, MagicMock(), MagicMock())

        result = await poller._try_claim()
        assert result is False

    async def test_try_claim_returns_true_when_task_claimed(self):
        config = WorkerConfig(worker_id="test-poller")
        task_id = uuid.uuid4()
        row = {
            "task_id": task_id,
            "tenant_id": "default",
            "agent_id": "test-agent",
            "status": "running",
            "retry_count": 0,
        }

        pool = MagicMock()
        conn = AsyncMock()
        conn.fetchrow = AsyncMock(return_value=row)
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=conn)
        ctx.__aexit__ = AsyncMock(return_value=False)
        pool.acquire = MagicMock(return_value=ctx)
        metrics = MetricsCollector()

        # No callback — semaphore released immediately
        poller = TaskPoller(config, pool, metrics, MagicMock(), None)

        result = await poller._try_claim()
        assert result is True
        assert metrics.get_counter("tasks.active", worker_id="test-poller") >= 1

    async def test_try_claim_invokes_callback(self):
        config = WorkerConfig(worker_id="test-poller")
        task_id = uuid.uuid4()
        row = {
            "task_id": task_id,
            "tenant_id": "default",
            "agent_id": "test-agent",
            "status": "running",
            "retry_count": 0,
        }

        pool = MagicMock()
        conn = AsyncMock()
        conn.fetchrow = AsyncMock(return_value=row)
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=conn)
        ctx.__aexit__ = AsyncMock(return_value=False)
        pool.acquire = MagicMock(return_value=ctx)
        metrics = MetricsCollector()

        callback_received = []

        class MockRouter:
            def get_executor(self, task_data: dict):
                class MockExecutor:
                    async def execute_task(self, td: dict, cancel_event):
                        callback_received.append(td)
                return MockExecutor()

        heartbeat = MagicMock()
        handle = MagicMock()
        handle.cancel_event = asyncio.Event()
        heartbeat.start_heartbeat.return_value = handle
        poller = TaskPoller(config, pool, metrics, heartbeat, MockRouter())

        result = await poller._try_claim()
        assert result is True

        # Wait for the async task to complete
        await asyncio.sleep(0.05)
        assert len(callback_received) == 1
        assert callback_received[0]["task_id"] == task_id


class TestPollerDrain:
    """Issue #15: drain() should wait for in-flight tasks before returning."""

    def _make_poller(self, config=None) -> "TaskPoller":
        config = config or WorkerConfig(worker_id="test-poller")
        pool = MagicMock()
        pool.acquire = MagicMock()
        metrics = MetricsCollector()
        heartbeat = MagicMock()
        return TaskPoller(config, pool, metrics, heartbeat, None)

    @pytest.mark.asyncio
    async def test_drain_returns_true_when_no_inflight_tasks(self):
        poller = self._make_poller()
        assert poller._active_tasks_count == 0
        result = await poller.drain(timeout=1.0)
        assert result is True

    @pytest.mark.asyncio
    async def test_drain_waits_until_tasks_finish(self):
        poller = self._make_poller()
        poller._active_tasks_count = 1

        async def finish_after_delay():
            await asyncio.sleep(0.1)
            poller._active_tasks_count = 0

        asyncio.create_task(finish_after_delay())
        result = await poller.drain(timeout=2.0)
        assert result is True

    @pytest.mark.asyncio
    async def test_drain_returns_false_on_timeout(self):
        poller = self._make_poller()
        poller._active_tasks_count = 1  # never cleared

        result = await poller.drain(timeout=0.2)
        assert result is False
