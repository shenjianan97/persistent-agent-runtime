"""Worker Service — top-level orchestrator for poller, heartbeat, and reaper.

Assembles the core primitives into a runnable service. Task 6 (Graph Executor)
provides the on_task_claimed callback that actually executes the LangGraph graph.
"""

from __future__ import annotations

import asyncio
import signal
from typing import Any, Awaitable, Callable

import asyncpg

from core.config import WorkerConfig
from core.db import create_pool
from core.heartbeat import HeartbeatManager
from core.logging import MetricsCollector, configure_logging, get_logger
from core.poller import TaskPoller
from core.reaper import ReaperTask


class WorkerService:
    """Top-level worker service that ties together poller, heartbeat, and reaper.

    Usage:
        config = WorkerConfig(db_dsn="postgresql://...")
        worker = WorkerService(config, on_task_claimed=my_executor)
        await worker.start()
        # ... runs until shutdown signal ...
        await worker.stop()

    The on_task_claimed callback receives a dict of the claimed task row and
    is responsible for:
      1. Starting a heartbeat via worker.heartbeat.start_heartbeat(task_id, tenant_id)
      2. Executing the graph
      3. Stopping the heartbeat via worker.heartbeat.stop_heartbeat(task_id)
    """

    def __init__(
        self,
        config: WorkerConfig | None = None,
        on_task_claimed: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> None:
        self._config = config or WorkerConfig()
        self._on_task_claimed = on_task_claimed
        self._pool: asyncpg.Pool | None = None
        self._metrics = MetricsCollector()
        self._log = get_logger(self._config.worker_id, component="worker")

        # These are initialized in start()
        self.poller: TaskPoller | None = None
        self.heartbeat: HeartbeatManager | None = None
        self.reaper: ReaperTask | None = None

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

        # Create connection pool
        self._pool = await create_pool(
            self._config.db_dsn,
            min_size=2,
            max_size=self._config.max_concurrent_tasks + 5,
        )

        # Initialize subsystems
        self.heartbeat = HeartbeatManager(
            self._config, self._pool, self._metrics
        )

        self.poller = TaskPoller(
            self._config,
            self._pool,
            self._metrics,
            on_task_claimed=self._on_task_claimed,
        )

        self.reaper = ReaperTask(
            self._config, self._pool, self._metrics
        )

        # Start subsystems
        await self.poller.start()
        await self.reaper.start()

        self._metrics.set_gauge(
            "workers.active_tasks",
            0,
            worker_id=self._config.worker_id,
        )

        await self._log.ainfo("worker_started")

    async def stop(self) -> None:
        """Gracefully shut down all subsystems."""
        await self._log.ainfo("worker_stopping")

        if self.poller:
            await self.poller.stop()
        if self.heartbeat:
            await self.heartbeat.stop_all()
        if self.reaper:
            await self.reaper.stop()
        if self._pool:
            await self._pool.close()

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
