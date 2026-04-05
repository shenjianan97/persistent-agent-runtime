"""Task Poller — claims queued tasks using agent-aware round-robin scheduling.

Primary wake mechanism: LISTEN new_task (PostgreSQL LISTEN/NOTIFY).
Fallback: periodic polling with exponential backoff on empty polls.
Concurrency bounded by asyncio.Semaphore(MAX_CONCURRENT_TASKS).

Scheduling model: round-robin across eligible agents within each worker_pool_id.
Agent eligibility requires: active status, running_task_count < max_concurrent_tasks,
hour_window_cost_microdollars < budget_max_per_hour, and queued tasks in the pool.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Callable, Awaitable

import asyncpg

from core.config import WorkerConfig
from core.logging import (
    POLL_EMPTY,
    TASK_CLAIMED,
    MetricsCollector,
    get_logger,
)


# SQL fragments for the agent-aware round-robin claim path.
# These are sequential queries executed within a single transaction.

# Step 0: Ensure all agents with queued tasks have runtime state rows.
_PRECLAIM_UPSERT_SQL = '''
INSERT INTO agent_runtime_state
    (tenant_id, agent_id, running_task_count, hour_window_cost_microdollars, scheduler_cursor, updated_at)
SELECT DISTINCT t.tenant_id, t.agent_id, 0, 0, '1970-01-01T00:00:00Z'::timestamptz, NOW()
FROM tasks t
WHERE t.worker_pool_id = $1 AND t.tenant_id = $2 AND t.status = 'queued'
ON CONFLICT DO NOTHING
'''

# Step 1: Find and lock the next eligible agent (oldest scheduler_cursor).
_FIND_ELIGIBLE_AGENT_SQL = '''
SELECT ars.tenant_id, ars.agent_id
FROM agent_runtime_state ars
JOIN agents a ON a.tenant_id = ars.tenant_id AND a.agent_id = ars.agent_id
WHERE a.status = 'active'
  AND ars.tenant_id = $2
  AND ars.running_task_count < a.max_concurrent_tasks
  AND ars.hour_window_cost_microdollars < a.budget_max_per_hour
  AND EXISTS (
      SELECT 1 FROM tasks t
      WHERE t.tenant_id = ars.tenant_id
        AND t.agent_id = ars.agent_id
        AND t.worker_pool_id = $1
        AND t.status = 'queued'
        AND (t.retry_after IS NULL OR t.retry_after < NOW())
  )
ORDER BY ars.scheduler_cursor ASC
LIMIT 1
FOR UPDATE OF ars
'''

# Step 2: Find and lock the chosen agent's oldest eligible queued task.
_FIND_AGENT_TASK_SQL = '''
SELECT task_id FROM tasks
WHERE tenant_id = $1 AND agent_id = $2
  AND worker_pool_id = $3
  AND status = 'queued'
  AND (retry_after IS NULL OR retry_after < NOW())
ORDER BY created_at ASC
LIMIT 1
FOR UPDATE SKIP LOCKED
'''

# Step 3: Claim the task (queued -> running).
_CLAIM_TASK_SQL = '''
UPDATE tasks
SET status = 'running',
    lease_owner = $1,
    lease_expiry = NOW() + $2 * INTERVAL '1 second',
    version = version + 1,
    updated_at = NOW()
WHERE task_id = $3
RETURNING *
'''

# Step 4: Increment running_task_count and advance scheduler_cursor.
_UPDATE_RUNTIME_STATE_SQL = '''
UPDATE agent_runtime_state
SET running_task_count = running_task_count + 1,
    scheduler_cursor = NOW(),
    updated_at = NOW()
WHERE tenant_id = $1 AND agent_id = $2
'''

# Step 5: Record task_claimed event.
_INSERT_TASK_EVENT_SQL = '''
INSERT INTO task_events (tenant_id, task_id, agent_id, event_type,
                         status_before, status_after, worker_id,
                         error_code, error_message, details)
VALUES ($1, $2::uuid, $3, $4, $5, $6, $7, $8, $9, $10::jsonb)
'''


class TaskPoller:
    """Polls the database for claimable tasks.

    Uses LISTEN/NOTIFY as the primary wake mechanism with fallback to
    exponential-backoff polling. Concurrency is bounded by a semaphore.

    The poller does not execute tasks itself — it invokes a user-provided
    callback (the executor hook from Task 6) for each claimed task.
    """

    def __init__(
        self,
        config: WorkerConfig,
        pool: asyncpg.Pool,
        metrics: MetricsCollector,
        heartbeat: Any,
        router: Any,
    ) -> None:
        self._config = config
        self._pool = pool
        self._metrics = metrics
        self._heartbeat = heartbeat
        self._router = router
        self._semaphore = asyncio.Semaphore(config.max_concurrent_tasks)
        self._log = get_logger(config.worker_id, component="poller")
        self._running = False
        self._backoff_ms = config.poll_backoff_initial_ms
        self._listen_conn: asyncpg.Connection | None = None
        self._notify_event = asyncio.Event()
        self._poll_task: asyncio.Task | None = None
        self._listen_task: asyncio.Task | None = None
        self._active_tasks_count = 0
        self._execution_tasks: set[asyncio.Task[None]] = set()

    @property
    def semaphore(self) -> asyncio.Semaphore:
        """Expose semaphore so external code (Task 6) can respect concurrency."""
        return self._semaphore

    @property
    def running(self) -> bool:
        return self._running

    @property
    def active_execution_tasks_count(self) -> int:
        return len(self._execution_tasks)

    @property
    def active_tasks_count(self) -> int:
        return self._active_tasks_count

    async def start(self) -> None:
        """Start the poller and LISTEN listener."""
        self._running = True
        self._listen_task = asyncio.create_task(self._listen_loop())
        self._poll_task = asyncio.create_task(self._poll_loop())
        await self._log.ainfo("poller_started", pool_id=self._config.worker_pool_id)

    async def drain(self, timeout: float) -> bool:
        """Wait for all in-flight tasks to finish. Returns True if fully drained before timeout."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while self._active_tasks_count > 0:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return False
            await asyncio.sleep(min(0.5, remaining))
        return True

    async def quiesce(self) -> None:
        """Stop accepting new tasks and let in-flight claim attempts finish."""
        self._running = False
        self._notify_event.set()  # Wake up any sleeping poll
        if self._poll_task:
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass

    async def stop(self) -> None:
        """Gracefully stop the poller."""
        await self.quiesce()
        if self._listen_task:
            self._listen_task.cancel()
            try:
                await self._listen_task
            except asyncio.CancelledError:
                pass
        if self._listen_conn and not self._listen_conn.is_closed():
            await self._listen_conn.close()
        await self._log.ainfo("poller_stopped")

    async def cancel_active_tasks(self) -> None:
        """Cancel in-flight execution tasks and wait for their cleanup to finish."""
        if not self._execution_tasks:
            return

        tasks = list(self._execution_tasks)
        await self._log.ainfo(
            "poller_cancel_active_tasks_started",
            active_execution_tasks_count=len(tasks),
        )
        for task in tasks:
            task.cancel()

        await asyncio.gather(*tasks, return_exceptions=True)
        self._execution_tasks.difference_update(tasks)
        await self._log.ainfo(
            "poller_cancel_active_tasks_completed",
            active_execution_tasks_count=self.active_execution_tasks_count,
        )

    async def _listen_loop(self) -> None:
        """Maintain a LISTEN connection and signal the poller on notifications."""
        while self._running:
            try:
                self._listen_conn = await asyncpg.connect(dsn=self._config.db_dsn)
                await self._listen_conn.add_listener("new_task", self._on_notify)
                await self._log.ainfo("listen_connected")
                # Keep connection alive until stopped or connection drops
                while self._running and not self._listen_conn.is_closed():
                    await asyncio.sleep(1.0)
            except (asyncpg.PostgresError, OSError, ConnectionError) as exc:
                await self._log.awarning("listen_connection_lost", error=str(exc))
                await asyncio.sleep(1.0)
            except asyncio.CancelledError:
                break
            finally:
                if self._listen_conn and not self._listen_conn.is_closed():
                    try:
                        await self._listen_conn.close()
                    except Exception:
                        pass

    def _on_notify(
        self,
        connection: asyncpg.Connection,
        pid: int,
        channel: str,
        payload: str,
    ) -> None:
        """Handle LISTEN notification — wake the poller."""
        # Only wake if the notification is for our pool or is unfiltered
        if payload == self._config.worker_pool_id or payload == "":
            self._notify_event.set()

    async def _poll_loop(self) -> None:
        """Main polling loop with exponential backoff on empty polls."""
        while self._running:
            try:
                # Wait for notification or backoff timeout
                try:
                    await asyncio.wait_for(
                        self._notify_event.wait(),
                        timeout=self._backoff_ms / 1000.0,
                    )
                except asyncio.TimeoutError:
                    pass  # Fallback poll on timeout

                self._notify_event.clear()

                if not self._running:
                    break

                # Try to claim tasks while we have capacity
                claimed = await self._try_claim()
                if claimed:
                    self._backoff_ms = self._config.poll_backoff_initial_ms
                else:
                    self._metrics.increment("poll.empty", worker_id=self._config.worker_id)
                    await self._log.adebug(POLL_EMPTY, backoff_ms=self._backoff_ms)
                    # Exponential backoff on empty poll
                    self._backoff_ms = min(
                        int(self._backoff_ms * self._config.poll_backoff_multiplier),
                        self._config.poll_backoff_max_ms,
                    )
            except asyncio.CancelledError:
                break
            except Exception as exc:
                await self._log.aerror("poll_error", error=str(exc), exc_info=True)
                await asyncio.sleep(1.0)

    async def _try_claim(self) -> bool:
        """Attempt to claim a single task using agent-aware round-robin.

        The claim selects the next eligible agent (oldest scheduler_cursor) and
        claims that agent's oldest queued task.  All state mutations — task status
        change, running_task_count increment, scheduler_cursor advance, and
        task_claimed event — happen in a single transaction.

        Returns True if a task was claimed.
        """
        # Check semaphore availability without blocking
        if self._active_tasks_count >= self._config.max_concurrent_tasks:
            return False

        await self._semaphore.acquire()
        try:
            task_data: dict[str, Any] | None = None
            async with self._pool.acquire() as conn:
                async with conn.transaction():
                    # Step 0: Pre-claim upsert — ensure agents with queued
                    # tasks have runtime state rows so newly created agents
                    # are visible to the scheduler.
                    await conn.execute(
                        _PRECLAIM_UPSERT_SQL,
                        self._config.worker_pool_id,
                        self._config.tenant_id,
                    )

                    # Step 1: Find and lock the next eligible agent
                    agent_row = await conn.fetchrow(
                        _FIND_ELIGIBLE_AGENT_SQL,
                        self._config.worker_pool_id,
                        self._config.tenant_id,
                    )
                    if agent_row is None:
                        # No eligible agents — release semaphore and back off
                        self._semaphore.release()
                        return False

                    # Step 2: Find and lock that agent's oldest queued task
                    task_row = await conn.fetchrow(
                        _FIND_AGENT_TASK_SQL,
                        agent_row['tenant_id'],
                        agent_row['agent_id'],
                        self._config.worker_pool_id,
                    )
                    if task_row is None:
                        # Task was claimed by another worker between steps
                        self._semaphore.release()
                        return False

                    # Step 3: Claim the task (queued -> running)
                    claimed = await conn.fetchrow(
                        _CLAIM_TASK_SQL,
                        self._config.worker_id,
                        self._config.lease_duration_seconds,
                        task_row['task_id'],
                    )

                    # Step 4: Increment running_task_count and advance cursor
                    await conn.execute(
                        _UPDATE_RUNTIME_STATE_SQL,
                        agent_row['tenant_id'],
                        agent_row['agent_id'],
                    )

                    # Step 5: Record task_claimed event
                    await conn.execute(
                        _INSERT_TASK_EVENT_SQL,
                        claimed['tenant_id'],
                        str(claimed['task_id']),
                        claimed['agent_id'],
                        "task_claimed",
                        "queued",
                        "running",
                        self._config.worker_id,
                        None,
                        None,
                        "{}",
                    )

                    task_data = dict(claimed)

            task_id = str(task_data["task_id"])

            self._active_tasks_count += 1
            self._metrics.increment("tasks.active", worker_id=self._config.worker_id)
            self._metrics.set_gauge(
                "workers.active_tasks",
                self._active_tasks_count,
                worker_id=self._config.worker_id,
            )

            await self._log.ainfo(
                TASK_CLAIMED,
                task_id=task_id,
                retry_count=task_data.get("retry_count", 0),
            )

            if self._router:
                # Launch task execution without blocking the poller.
                # The semaphore is released by the executor when the task finishes.
                execution_task = asyncio.create_task(self._execute_and_release(task_data))
                self._execution_tasks.add(execution_task)
                execution_task.add_done_callback(self._execution_tasks.discard)
            else:
                # No executor callback — release immediately (useful in tests)
                self._semaphore.release()

            return True

        except Exception:
            self._active_tasks_count = max(0, self._active_tasks_count - 1)
            self._semaphore.release()
            raise

    async def _execute_and_release(self, task_data: dict[str, Any]) -> None:
        """Route the task, wrap it in a heartbeat lease, execute, and release the semaphore."""
        task_id = str(task_data["task_id"])
        tenant_id = task_data["tenant_id"]
        
        # 1. Coordination: Ask HeartbeatManager to maintain lease in the background
        handle = self._heartbeat.start_heartbeat(task_id, tenant_id)
        try:
            if self._router:
                # 2. Routing: Ask TaskRouter to pick an executor based on task_data
                executor = self._router.get_executor(task_data)
                
                # 3. Execution: Run the graph, passing the cancellation event so it can abort
                await executor.execute_task(task_data, handle.cancel_event)
        except Exception as exc:
            await self._log.aerror(
                "task_execution_error",
                task_id=task_id,
                error=str(exc),
                exc_info=True,
            )
        finally:
            # 4. Cleanup: Tell HeartbeatManager to stop pinging, and release semaphore
            await self._heartbeat.stop_heartbeat(task_id)
            self._active_tasks_count = max(0, self._active_tasks_count - 1)
            self._semaphore.release()
            self._metrics.set_gauge(
                "workers.active_tasks",
                self._active_tasks_count,
                worker_id=self._config.worker_id,
            )

    def reset_backoff(self) -> None:
        """Reset backoff to initial value (e.g., after successful claim)."""
        self._backoff_ms = self._config.poll_backoff_initial_ms
