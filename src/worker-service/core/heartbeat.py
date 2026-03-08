"""Heartbeat Manager — extends leases for active tasks.

Runs every 15s per active task, extending lease_expiry by 60s.
If the UPDATE returns 0 rows, the lease was revoked and the
corresponding executor is signalled to stop.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, Callable

import asyncpg

from core.config import WorkerConfig
from core.logging import (
    HEARTBEAT_SENT,
    LEASE_REVOKED,
    MetricsCollector,
    get_logger,
)

if TYPE_CHECKING:
    pass

# Exact heartbeat query from design/PHASE1_DURABLE_EXECUTION.md Section 6.1
HEARTBEAT_QUERY = """
UPDATE tasks
SET lease_expiry = NOW() + INTERVAL '60 seconds',
    updated_at = NOW()
WHERE task_id = $1
  AND tenant_id = $2
  AND lease_owner = $3
  AND status = 'running';
"""


class HeartbeatHandle:
    """Handle for a single task's heartbeat.

    Created by HeartbeatManager.start_heartbeat() and used to stop
    heartbeating when the task finishes or is revoked.
    """

    def __init__(
        self,
        task_id: str,
        cancel_event: asyncio.Event,
        heartbeat_task: asyncio.Task,
    ) -> None:
        self.task_id = task_id
        self.cancel_event = cancel_event
        self._heartbeat_task = heartbeat_task
        self.lease_revoked = False

    async def stop(self) -> None:
        """Stop heartbeating for this task."""
        self._heartbeat_task.cancel()
        try:
            await self._heartbeat_task
        except asyncio.CancelledError:
            pass


class HeartbeatManager:
    """Manages heartbeats for all active tasks on this worker.

    For each active task, a separate asyncio task sends heartbeat UPDATEs
    every heartbeat_interval_seconds. If a heartbeat UPDATE returns 0 rows,
    the lease was revoked — the manager sets a cancellation event so the
    graph executor can stop.
    """

    def __init__(
        self,
        config: WorkerConfig,
        pool: asyncpg.Pool,
        metrics: MetricsCollector,
        on_lease_revoked: Callable[[str], Any] | None = None,
    ) -> None:
        self._config = config
        self._pool = pool
        self._metrics = metrics
        self._on_lease_revoked = on_lease_revoked
        self._log = get_logger(config.worker_id, component="heartbeat")
        self._active: dict[str, HeartbeatHandle] = {}

    @property
    def active_tasks(self) -> dict[str, HeartbeatHandle]:
        """Return the dict of active heartbeat handles keyed by task_id."""
        return self._active

    def start_heartbeat(self, task_id: str, tenant_id: str) -> HeartbeatHandle:
        """Start heartbeating for a task. Returns a handle to stop it later.

        Args:
            task_id: The task to heartbeat for.
            tenant_id: The tenant that owns the task.

        Returns:
            A HeartbeatHandle that can be used to stop the heartbeat
            and check for lease revocation.
        """
        cancel_event = asyncio.Event()
        heartbeat_task = asyncio.create_task(
            self._heartbeat_loop(task_id, tenant_id, cancel_event)
        )
        handle = HeartbeatHandle(task_id, cancel_event, heartbeat_task)
        self._active[task_id] = handle
        return handle

    async def stop_heartbeat(self, task_id: str) -> None:
        """Stop heartbeating for a specific task."""
        handle = self._active.pop(task_id, None)
        if handle:
            await handle.stop()

    async def stop_all(self) -> None:
        """Stop all active heartbeats."""
        tasks = list(self._active.keys())
        for task_id in tasks:
            await self.stop_heartbeat(task_id)

    async def _heartbeat_loop(
        self,
        task_id: str,
        tenant_id: str,
        cancel_event: asyncio.Event,
    ) -> None:
        """Send heartbeats at the configured interval until stopped."""
        try:
            while True:
                await asyncio.sleep(self._config.heartbeat_interval_seconds)

                try:
                    async with self._pool.acquire() as conn:
                        result = await conn.execute(
                            HEARTBEAT_QUERY,
                            task_id,
                            tenant_id,
                            self._config.worker_id,
                        )

                    # asyncpg execute returns a status string like "UPDATE N"
                    rows_updated = int(result.split()[-1])

                    if rows_updated == 0:
                        # Lease was revoked
                        await self._log.awarning(
                            LEASE_REVOKED,
                            task_id=task_id,
                        )
                        self._metrics.increment(
                            "heartbeats.missed",
                            worker_id=self._config.worker_id,
                        )

                        handle = self._active.get(task_id)
                        if handle:
                            handle.lease_revoked = True
                            handle.cancel_event.set()

                        if self._on_lease_revoked:
                            self._on_lease_revoked(task_id)

                        break
                    else:
                        await self._log.adebug(
                            HEARTBEAT_SENT,
                            task_id=task_id,
                        )

                except (asyncpg.PostgresError, OSError) as exc:
                    await self._log.aerror(
                        "heartbeat_error",
                        task_id=task_id,
                        error=str(exc),
                    )
                    # Continue trying — transient DB errors shouldn't stop heartbeating

        except asyncio.CancelledError:
            pass
        finally:
            self._active.pop(task_id, None)
