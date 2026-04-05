"""Tests for TaskPoller — agent-aware round-robin claim, LISTEN/NOTIFY, backoff behavior."""

import asyncio
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.config import WorkerConfig
from core.logging import MetricsCollector
from core.poller import (
    _PRECLAIM_UPSERT_SQL,
    _FIND_ELIGIBLE_AGENT_SQL,
    _FIND_AGENT_TASK_SQL,
    _CLAIM_TASK_SQL,
    _UPDATE_RUNTIME_STATE_SQL,
    _INSERT_TASK_EVENT_SQL,
    TaskPoller,
)


class TestSchedulerClaimSQL:
    """Verify the round-robin claim SQL fragments match the design doc contract."""

    def test_preclaim_upsert_targets_agent_runtime_state(self):
        assert "agent_runtime_state" in _PRECLAIM_UPSERT_SQL

    def test_preclaim_upsert_filters_queued_tasks(self):
        assert "status = 'queued'" in _PRECLAIM_UPSERT_SQL

    def test_preclaim_upsert_uses_on_conflict(self):
        assert "ON CONFLICT DO NOTHING" in _PRECLAIM_UPSERT_SQL

    def test_preclaim_upsert_filters_by_pool_and_tenant(self):
        assert "worker_pool_id = $1" in _PRECLAIM_UPSERT_SQL
        assert "tenant_id = $2" in _PRECLAIM_UPSERT_SQL

    def test_eligible_agent_joins_agents_table(self):
        assert "JOIN agents a" in _FIND_ELIGIBLE_AGENT_SQL

    def test_eligible_agent_checks_active_status(self):
        assert "a.status = 'active'" in _FIND_ELIGIBLE_AGENT_SQL

    def test_eligible_agent_checks_concurrency_limit(self):
        assert "running_task_count < a.max_concurrent_tasks" in _FIND_ELIGIBLE_AGENT_SQL

    def test_eligible_agent_checks_hourly_budget(self):
        assert "hour_window_cost_microdollars < a.budget_max_per_hour" in _FIND_ELIGIBLE_AGENT_SQL

    def test_eligible_agent_checks_queued_tasks_exist(self):
        assert "EXISTS" in _FIND_ELIGIBLE_AGENT_SQL
        assert "status = 'queued'" in _FIND_ELIGIBLE_AGENT_SQL

    def test_eligible_agent_checks_retry_after(self):
        assert "t.retry_after IS NULL OR t.retry_after < NOW()" in _FIND_ELIGIBLE_AGENT_SQL

    def test_eligible_agent_orders_by_scheduler_cursor(self):
        assert "ORDER BY ars.scheduler_cursor ASC" in _FIND_ELIGIBLE_AGENT_SQL

    def test_eligible_agent_uses_for_update_of_ars(self):
        assert "FOR UPDATE OF ars" in _FIND_ELIGIBLE_AGENT_SQL

    def test_eligible_agent_filters_by_pool_and_tenant(self):
        assert "worker_pool_id = $1" in _FIND_ELIGIBLE_AGENT_SQL
        assert "ars.tenant_id = $2" in _FIND_ELIGIBLE_AGENT_SQL

    def test_find_task_has_for_update_skip_locked(self):
        assert "FOR UPDATE SKIP LOCKED" in _FIND_AGENT_TASK_SQL

    def test_find_task_checks_retry_after(self):
        assert "retry_after IS NULL OR retry_after < NOW()" in _FIND_AGENT_TASK_SQL

    def test_find_task_orders_by_created_at(self):
        assert "ORDER BY created_at ASC" in _FIND_AGENT_TASK_SQL

    def test_find_task_filters_by_pool_and_agent(self):
        assert "tenant_id = $1" in _FIND_AGENT_TASK_SQL
        assert "agent_id = $2" in _FIND_AGENT_TASK_SQL
        assert "worker_pool_id = $3" in _FIND_AGENT_TASK_SQL

    def test_claim_task_sets_running(self):
        assert "status = 'running'" in _CLAIM_TASK_SQL

    def test_claim_task_sets_lease(self):
        assert "lease_owner = $1" in _CLAIM_TASK_SQL
        assert "lease_expiry" in _CLAIM_TASK_SQL

    def test_claim_task_increments_version(self):
        assert "version = version + 1" in _CLAIM_TASK_SQL

    def test_claim_task_returns_full_row(self):
        assert "RETURNING *" in _CLAIM_TASK_SQL

    def test_update_runtime_state_increments_running_count(self):
        assert "running_task_count = running_task_count + 1" in _UPDATE_RUNTIME_STATE_SQL

    def test_update_runtime_state_advances_cursor(self):
        assert "scheduler_cursor = NOW()" in _UPDATE_RUNTIME_STATE_SQL

    def test_insert_task_event_matches_pattern(self):
        assert "task_events" in _INSERT_TASK_EVENT_SQL
        assert "event_type" in _INSERT_TASK_EVENT_SQL


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
    """Test the _try_claim method with mocked database.

    The new claim path makes multiple sequential fetchrow calls within a single
    transaction.  The mock conn's fetchrow is configured via side_effect to
    return different values for the agent-eligibility step, the task-find step,
    and the claim-update step.
    """

    @staticmethod
    def _make_poller_conn(fetchrow_side_effect=None, fetchrow_return=None):
        """Create a mock conn with transaction() support for poller tests.

        For the new round-robin claim, fetchrow is called 3 times in succession
        (find agent, find task, claim task).  Use ``fetchrow_side_effect`` to
        provide a list of return values for each call.  If only
        ``fetchrow_return`` is given, all calls return the same value (backward
        compat for simple cases).
        """
        conn = AsyncMock()
        if fetchrow_side_effect is not None:
            conn.fetchrow = AsyncMock(side_effect=fetchrow_side_effect)
        else:
            conn.fetchrow = AsyncMock(return_value=fetchrow_return)
        conn.execute = AsyncMock()
        conn.fetch = AsyncMock(return_value=[])
        conn.fetchval = AsyncMock(return_value=None)
        # transaction() returns a sync object usable as async context manager
        tx = AsyncMock()
        tx.__aenter__ = AsyncMock(return_value=None)
        tx.__aexit__ = AsyncMock(return_value=False)
        conn.transaction = MagicMock(return_value=tx)
        return conn

    async def test_try_claim_returns_false_when_no_eligible_agent(self):
        """When step 1 (find eligible agent) returns None, claim should fail."""
        config = WorkerConfig(worker_id="test-poller")
        # Step 1 returns None → no eligible agent
        conn = self._make_poller_conn(fetchrow_side_effect=[None])
        pool = MagicMock()
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=conn)
        ctx.__aexit__ = AsyncMock(return_value=False)
        pool.acquire = MagicMock(return_value=ctx)
        metrics = MetricsCollector()
        poller = TaskPoller(config, pool, metrics, MagicMock(), MagicMock())

        result = await poller._try_claim()
        assert result is False

    async def test_try_claim_returns_false_when_no_task_found(self):
        """When step 2 (find task) returns None, claim should fail."""
        config = WorkerConfig(worker_id="test-poller")
        agent_row = {"tenant_id": "default", "agent_id": "agent-1"}
        # Step 1 returns agent, Step 2 returns None (no task)
        conn = self._make_poller_conn(fetchrow_side_effect=[agent_row, None])
        pool = MagicMock()
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
        agent_row = {"tenant_id": "default", "agent_id": "test-agent"}
        task_row = {"task_id": task_id}
        claimed_row = {
            "task_id": task_id,
            "tenant_id": "default",
            "agent_id": "test-agent",
            "status": "running",
            "retry_count": 0,
        }

        # Step 1: agent, Step 2: task, Step 3: claimed row
        conn = self._make_poller_conn(
            fetchrow_side_effect=[agent_row, task_row, claimed_row]
        )
        pool = MagicMock()
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

    async def test_try_claim_executes_preclaim_upsert(self):
        """Verify the pre-claim upsert (step 0) is called before finding agents."""
        config = WorkerConfig(worker_id="test-poller", worker_pool_id="pool-1", tenant_id="t1")
        # No eligible agent
        conn = self._make_poller_conn(fetchrow_side_effect=[None])
        pool = MagicMock()
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=conn)
        ctx.__aexit__ = AsyncMock(return_value=False)
        pool.acquire = MagicMock(return_value=ctx)
        metrics = MetricsCollector()
        poller = TaskPoller(config, pool, metrics, MagicMock(), MagicMock())

        await poller._try_claim()

        # The first conn.execute call should be the pre-claim upsert
        first_execute_call = conn.execute.call_args_list[0]
        assert "agent_runtime_state" in first_execute_call[0][0]
        assert first_execute_call[0][1] == "pool-1"
        assert first_execute_call[0][2] == "t1"

    async def test_try_claim_updates_runtime_state_and_inserts_event(self):
        """Verify step 4 (runtime state update) and step 5 (event insert) are called."""
        config = WorkerConfig(worker_id="test-poller")
        task_id = uuid.uuid4()
        agent_row = {"tenant_id": "default", "agent_id": "test-agent"}
        task_row = {"task_id": task_id}
        claimed_row = {
            "task_id": task_id,
            "tenant_id": "default",
            "agent_id": "test-agent",
            "status": "running",
            "retry_count": 0,
        }

        conn = self._make_poller_conn(
            fetchrow_side_effect=[agent_row, task_row, claimed_row]
        )
        pool = MagicMock()
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=conn)
        ctx.__aexit__ = AsyncMock(return_value=False)
        pool.acquire = MagicMock(return_value=ctx)
        metrics = MetricsCollector()
        poller = TaskPoller(config, pool, metrics, MagicMock(), None)

        await poller._try_claim()

        # conn.execute is called 3 times: pre-claim upsert, runtime state update, event insert
        assert conn.execute.call_count == 3

        # Step 4: runtime state update
        runtime_call = conn.execute.call_args_list[1]
        assert "running_task_count = running_task_count + 1" in runtime_call[0][0]
        assert runtime_call[0][1] == "default"
        assert runtime_call[0][2] == "test-agent"

        # Step 5: event insert
        event_call = conn.execute.call_args_list[2]
        assert "task_events" in event_call[0][0]
        assert event_call[0][4] == "task_claimed"

    async def test_try_claim_invokes_callback(self):
        config = WorkerConfig(worker_id="test-poller")
        task_id = uuid.uuid4()
        agent_row = {"tenant_id": "default", "agent_id": "test-agent"}
        task_row = {"task_id": task_id}
        claimed_row = {
            "task_id": task_id,
            "tenant_id": "default",
            "agent_id": "test-agent",
            "status": "running",
            "retry_count": 0,
        }

        conn = self._make_poller_conn(
            fetchrow_side_effect=[agent_row, task_row, claimed_row]
        )
        pool = MagicMock()
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

    async def test_try_claim_materializes_row_before_connection_release(self):
        config = WorkerConfig(worker_id="test-poller")
        task_id = uuid.uuid4()

        class ConnectionBoundRow:
            def __init__(self, data):
                self._data = data
                self._closed = False

            def mark_closed(self):
                self._closed = True

            def _ensure_open(self):
                if self._closed:
                    raise RuntimeError("row accessed after connection released")

            def __getitem__(self, key):
                self._ensure_open()
                return self._data[key]

            def get(self, key, default=None):
                self._ensure_open()
                return self._data.get(key, default)

            def keys(self):
                self._ensure_open()
                return self._data.keys()

        agent_row = {"tenant_id": "default", "agent_id": "test-agent"}
        task_row = {"task_id": task_id}
        claimed_row = ConnectionBoundRow(
            {
                "task_id": task_id,
                "tenant_id": "default",
                "agent_id": "test-agent",
                "status": "running",
                "retry_count": 0,
            }
        )

        conn = self._make_poller_conn(
            fetchrow_side_effect=[agent_row, task_row, claimed_row]
        )
        pool = MagicMock()
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=conn)

        async def _close_connection(exc_type=None, exc=None, tb=None):
            del exc_type, exc, tb
            claimed_row.mark_closed()
            return False

        ctx.__aexit__ = AsyncMock(side_effect=_close_connection)
        pool.acquire = MagicMock(return_value=ctx)
        metrics = MetricsCollector()

        poller = TaskPoller(config, pool, metrics, MagicMock(), None)

        result = await poller._try_claim()

        assert result is True


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

    @pytest.mark.asyncio
    async def test_quiesce_waits_for_inflight_claim_attempt(self):
        """Issue #17: quiesce should not cancel claim attempt mid-flight."""
        poller = self._make_poller()
        poller._running = True
        poller._notify_event.set()
        entered_try_claim = asyncio.Event()
        release_try_claim = asyncio.Event()

        async def blocking_try_claim() -> bool:
            entered_try_claim.set()
            await release_try_claim.wait()
            return False

        poller._try_claim = blocking_try_claim  # type: ignore[method-assign]
        poller._poll_task = asyncio.create_task(poller._poll_loop())

        await entered_try_claim.wait()
        quiesce_task = asyncio.create_task(poller.quiesce())
        await asyncio.sleep(0.05)
        assert not quiesce_task.done()

        release_try_claim.set()
        await quiesce_task
        assert poller._poll_task.done()

    @pytest.mark.asyncio
    async def test_cancel_active_tasks_cancels_running_execution_tasks(self):
        config = WorkerConfig(worker_id="test-poller")
        task_id = uuid.uuid4()
        agent_row = {"tenant_id": "default", "agent_id": "test-agent"}
        task_row_data = {"task_id": task_id}
        claimed_row = {
            "task_id": task_id,
            "tenant_id": "default",
            "agent_id": "test-agent",
            "status": "running",
            "retry_count": 0,
        }

        conn = TestPollerTryClaim._make_poller_conn(
            fetchrow_side_effect=[agent_row, task_row_data, claimed_row]
        )
        pool = MagicMock()
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=conn)
        ctx.__aexit__ = AsyncMock(return_value=False)
        pool.acquire = MagicMock(return_value=ctx)
        metrics = MetricsCollector()

        started = asyncio.Event()
        cleaned_up = asyncio.Event()

        class MockExecutor:
            async def execute_task(self, td: dict, cancel_event):
                del td, cancel_event
                started.set()
                try:
                    await asyncio.Future()
                finally:
                    cleaned_up.set()

        class MockRouter:
            def get_executor(self, task_data: dict):
                del task_data
                return MockExecutor()

        heartbeat = MagicMock()
        handle = MagicMock()
        handle.cancel_event = asyncio.Event()
        heartbeat.start_heartbeat.return_value = handle
        heartbeat.stop_heartbeat = AsyncMock()

        poller = TaskPoller(config, pool, metrics, heartbeat, MockRouter())
        poller._log = MagicMock()
        poller._log.ainfo = AsyncMock()

        result = await poller._try_claim()
        assert result is True
        await started.wait()

        await poller.cancel_active_tasks()

        await asyncio.wait_for(cleaned_up.wait(), timeout=1.0)
        heartbeat.stop_heartbeat.assert_awaited_once_with(str(task_id))
        assert poller._active_tasks_count == 0
        poller._log.ainfo.assert_any_await(
            "poller_cancel_active_tasks_started",
            active_execution_tasks_count=1,
        )
        poller._log.ainfo.assert_any_await(
            "poller_cancel_active_tasks_completed",
            active_execution_tasks_count=0,
        )
