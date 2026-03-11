"""Task Poller — claims queued tasks using FOR UPDATE SKIP LOCKED.

Primary wake mechanism: LISTEN new_task (PostgreSQL LISTEN/NOTIFY).
Fallback: periodic polling with exponential backoff on empty polls.
Concurrency bounded by asyncio.Semaphore(MAX_CONCURRENT_TASKS).
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, Callable, Awaitable

import asyncpg

from core.config import WorkerConfig
from core.logging import (
    POLL_EMPTY,
    TASK_CLAIMED,
    MetricsCollector,
    get_logger,
)

if TYPE_CHECKING:
    pass

# Exact claim query from docs/design/PHASE1_DURABLE_EXECUTION.md Section 6.1
def build_claim_query(lease_duration_seconds: int) -> str:
    return f"""
WITH claimable AS (
    SELECT task_id
    FROM tasks
    WHERE status = 'queued'
      AND worker_pool_id = $1
      AND tenant_id = $2
      AND (retry_after IS NULL OR retry_after < NOW())
    ORDER BY created_at
    LIMIT 1
    FOR UPDATE SKIP LOCKED
)
UPDATE tasks t
SET status = 'running',
    lease_owner = $3,
    lease_expiry = NOW() + INTERVAL '{lease_duration_seconds} seconds',
    version = t.version + 1,
    updated_at = NOW()
FROM claimable c
WHERE t.task_id = c.task_id
RETURNING t.*;
"""


CLAIM_QUERY = build_claim_query(60)


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

    @property
    def semaphore(self) -> asyncio.Semaphore:
        """Expose semaphore so external code (Task 6) can respect concurrency."""
        return self._semaphore

    @property
    def running(self) -> bool:
        return self._running

    async def start(self) -> None:
        """Start the poller and LISTEN listener."""
        self._running = True
        self._listen_task = asyncio.create_task(self._listen_loop())
        self._poll_task = asyncio.create_task(self._poll_loop())
        await self._log.ainfo("poller_started", pool_id=self._config.worker_pool_id)

    async def stop(self) -> None:
        """Gracefully stop the poller."""
        self._running = False
        self._notify_event.set()  # Wake up any sleeping poll
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
        if self._listen_task:
            self._listen_task.cancel()
            try:
                await self._listen_task
            except asyncio.CancelledError:
                pass
        if self._listen_conn and not self._listen_conn.is_closed():
            await self._listen_conn.close()
        await self._log.ainfo("poller_stopped")

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
        """Attempt to claim a single task. Returns True if a task was claimed."""
        # Check semaphore availability without blocking
        if self._semaphore.locked() and self._semaphore._value == 0:  # type: ignore[attr-defined]
            return False

        await self._semaphore.acquire()
        try:
            async with self._pool.acquire() as conn:
                row = await conn.fetchrow(
                    build_claim_query(self._config.lease_duration_seconds),
                    self._config.worker_pool_id,
                    self._config.tenant_id,
                    self._config.worker_id,
                )

            if row is None:
                self._semaphore.release()
                return False

            task_data = dict(row)
            task_id = str(task_data["task_id"])

            self._metrics.increment("tasks.active", worker_id=self._config.worker_id)
            self._metrics.set_gauge(
                "workers.active_tasks",
                self._config.max_concurrent_tasks - self._semaphore._value,  # type: ignore[attr-defined]
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
                asyncio.create_task(
                    self._execute_and_release(task_data)
                )
            else:
                # No executor callback — release immediately (useful in tests)
                self._semaphore.release()

            return True

        except Exception:
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
            self._semaphore.release()
            active = self._config.max_concurrent_tasks - self._semaphore._value  # type: ignore[attr-defined]
            self._metrics.set_gauge(
                "workers.active_tasks",
                max(0, active),
                worker_id=self._config.worker_id,
            )

    def reset_backoff(self) -> None:
        """Reset backoff to initial value (e.g., after successful claim)."""
        self._backoff_ms = self._config.poll_backoff_initial_ms
