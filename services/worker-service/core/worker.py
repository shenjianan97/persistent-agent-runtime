"""Worker Service — top-level orchestrator for poller, heartbeat, and reaper.

Assembles the core primitives into a runnable service. Task 6 (Graph Executor)
provides the on_task_claimed callback that actually executes the LangGraph graph.
"""

from __future__ import annotations

import asyncio
import signal
from typing import TYPE_CHECKING, Any

import asyncpg

if TYPE_CHECKING:
    from executor.router import TaskRouter

from core.config import WorkerConfig
from core.heartbeat import HeartbeatManager
from core.logging import MetricsCollector, configure_logging, get_logger
from core.poller import TaskPoller
from core.reaper import ReaperTask


class WorkerService:
    """Top-level worker service that ties together poller, heartbeat, and reaper.

    Usage:
        pool = await create_pool(DB_DSN)
        router = DefaultTaskRouter(config, pool)
        worker = WorkerService(config, pool, router)
        await worker.start()
        # ... runs until shutdown signal ...
        await worker.stop()

    Architecture:
      - WorkerService is the "Engine Block" that holds the subsystems.
      - TaskPoller: Pulls queued tasks and assigns them coordinates.
      - HeartbeatManager: Pings the DB to maintain leases for active tasks.
      - ReaperTask: Recycles expired tasks from crashed workers.
      - TaskRouter: Routes claimed tasks to the correct executor.
    """

    def __init__(
        self,
        config: WorkerConfig,
        pool: asyncpg.Pool,
        router: "TaskRouter",
    ) -> None:
        self._config = config
        self._pool = pool
        self._router = router
        self._metrics = MetricsCollector()
        self._log = get_logger(self._config.worker_id, component="worker")

        # These are initialized in start()
        self.poller: TaskPoller | None = None
        self.heartbeat: HeartbeatManager | None = None
        self.reaper: ReaperTask | None = None
        self._registry_task: asyncio.Task | None = None

        self._shutdown_event = asyncio.Event()

    @property
    def config(self) -> WorkerConfig:
        return self._config

    @property
    def metrics(self) -> MetricsCollector:
        return self._metrics

    @property
    def pool(self) -> asyncpg.Pool | None:
        return self._pool

    async def start(self) -> None:
        """Initialize connections and start all subsystems."""
        configure_logging()

        await self._log.ainfo(
            "worker_starting",
            worker_id=self._config.worker_id,
            pool_id=self._config.worker_pool_id,
            max_concurrent=self._config.max_concurrent_tasks,
        )

        # Initialize subsystems
        self.heartbeat = HeartbeatManager(
            self._config, self._pool, self._metrics
        )

        self.poller = TaskPoller(
            self._config,
            self._pool,
            self._metrics,
            self.heartbeat,
            self._router,
        )

        self.reaper = ReaperTask(
            self._config, self._pool, self._metrics
        )

        # Register this worker in the workers table
        await self._register_worker()

        # Start subsystems
        await self.poller.start()
        await self.reaper.start()

        # Start periodic worker heartbeat (separate from task heartbeats)
        self._registry_task = asyncio.create_task(self._worker_heartbeat_loop())

        self._metrics.set_gauge(
            "workers.active_tasks",
            0,
            worker_id=self._config.worker_id,
        )

        await self._log.ainfo("worker_started")

    async def stop(self) -> None:
        """Gracefully shut down all subsystems."""
        await self._log.ainfo("worker_stopping")

        if self._registry_task:
            self._registry_task.cancel()
            try:
                await self._registry_task
            except asyncio.CancelledError:
                pass

        # Stop accepting new tasks first.
        if self.poller:
            await self.poller.stop()

        # Drain in-flight tasks before revoking heartbeats so workers don't burn
        # retry attempts on every ECS deployment.
        if self.poller:
            drained = await self.poller.drain(self._config.shutdown_drain_seconds)
            if not drained:
                await self._log.awarn(
                    "drain_timeout",
                    shutdown_drain_seconds=self._config.shutdown_drain_seconds,
                )

        if self.heartbeat:
            await self.heartbeat.stop_all()
        if self.reaper:
            await self.reaper.stop()

        await self._deregister_worker()
        await self._log.ainfo("worker_stopped")

    async def run_until_shutdown(self) -> None:
        """Run the worker until a shutdown signal (SIGTERM/SIGINT) is received."""
        loop = asyncio.get_running_loop()

        def _signal_handler() -> None:
            self._shutdown_event.set()

        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, _signal_handler)

        await self.start()
        try:
            await self._shutdown_event.wait()
        finally:
            await self.stop()

    async def _register_worker(self) -> None:
        """Register this worker in the workers table via upsert."""
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO workers (worker_id, worker_pool_id, tenant_id, status, last_heartbeat_at, started_at)
                    VALUES ($1, $2, $3, 'online', NOW(), NOW())
                    ON CONFLICT (worker_id) DO UPDATE
                    SET status = 'online', last_heartbeat_at = NOW(), started_at = NOW()
                    """,
                    self._config.worker_id,
                    self._config.worker_pool_id,
                    self._config.tenant_id,
                )
            await self._log.ainfo("worker_registered", worker_id=self._config.worker_id)
        except Exception as exc:
            await self._log.aerror("worker_register_failed", error=str(exc))

    async def _deregister_worker(self) -> None:
        """Mark this worker as offline in the workers table."""
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    "UPDATE workers SET status = 'offline', last_heartbeat_at = NOW() WHERE worker_id = $1",
                    self._config.worker_id,
                )
            await self._log.ainfo("worker_deregistered", worker_id=self._config.worker_id)
        except Exception as exc:
            await self._log.aerror("worker_deregister_failed", error=str(exc))

    async def _worker_heartbeat_loop(self) -> None:
        """Periodically update last_heartbeat_at so the API knows we're alive."""
        try:
            while True:
                await asyncio.sleep(self._config.heartbeat_interval_seconds)
                try:
                    async with self._pool.acquire() as conn:
                        await conn.execute(
                            "UPDATE workers SET status = 'online', last_heartbeat_at = NOW() WHERE worker_id = $1",
                            self._config.worker_id,
                        )
                except Exception as exc:
                    await self._log.aerror("worker_heartbeat_failed", error=str(exc))
        except asyncio.CancelledError:
            pass
