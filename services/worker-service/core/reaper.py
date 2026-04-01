"""Distributed Reaper — reclaims expired leases and timed-out tasks.

Runs on every worker instance at a jittered interval (30s +/- 10s).
Handles two conditions:
  (a) Expired leases: requeue with retry_count++ or dead-letter if exhausted.
  (b) Task timeouts: dead-letter with reason 'task_timeout'.
Both requeue paths emit pg_notify('new_task', worker_pool_id) in the same txn.
"""

from __future__ import annotations

import asyncio
import random

import asyncpg

from core.config import WorkerConfig
from core.logging import (
    REAPER_DEAD_LETTERED,
    REAPER_LEASE_EXPIRED,
    REAPER_TASK_TIMEOUT,
    MetricsCollector,
    get_logger,
)

# Exact reaper queries from docs/design/phase-1/PHASE1_DURABLE_EXECUTION.md Section 6.1

# Reaper — expired leases, requeue (retry_count < max_retries)
REAPER_REQUEUE_QUERY = """
WITH requeued AS (
    UPDATE tasks
    SET status = 'queued',
        lease_owner = NULL,
        lease_expiry = NULL,
        retry_count = retry_count + 1,
        retry_after = NOW() + (POWER(2, retry_count) * INTERVAL '1 second'),
        retry_history = retry_history || jsonb_build_array(NOW()),
        version = version + 1,
        updated_at = NOW()
    WHERE status = 'running'
      AND lease_expiry < NOW()
      AND retry_count < max_retries
    RETURNING task_id, worker_pool_id
)
, notified AS (
    SELECT pg_notify('new_task', worker_pool_id)
    FROM requeued
)
SELECT task_id
FROM requeued;
"""

# Reaper — expired leases, dead-letter (retry_count >= max_retries)
REAPER_DEAD_LETTER_QUERY = """
UPDATE tasks
SET status = 'dead_letter',
    last_worker_id = lease_owner,
    lease_owner = NULL,
    lease_expiry = NULL,
    last_error_code = 'retries_exhausted',
    last_error_message = 'max retries reached after lease expiry',
    dead_letter_reason = 'retries_exhausted',
    dead_lettered_at = NOW(),
    version = version + 1,
    updated_at = NOW()
WHERE status = 'running'
  AND lease_expiry < NOW()
  AND retry_count >= max_retries
RETURNING task_id;
"""

# Reaper — task timeout scan
REAPER_TIMEOUT_QUERY = """
UPDATE tasks
SET status = 'dead_letter',
    last_worker_id = lease_owner,
    lease_owner = NULL,
    lease_expiry = NULL,
    last_error_code = 'task_timeout',
    last_error_message = 'task exceeded task_timeout_seconds',
    dead_letter_reason = 'task_timeout',
    dead_lettered_at = NOW(),
    version = version + 1,
    updated_at = NOW()
WHERE status IN ('running', 'queued')
  AND timeout_reference_at + (task_timeout_seconds * INTERVAL '1 second') < NOW()
RETURNING task_id;
"""

# Queue depth query for metrics
QUEUE_DEPTH_QUERY = """
SELECT COUNT(*) as depth
FROM tasks
WHERE status = 'queued';
"""

# Mark workers as offline if no heartbeat for 90 seconds (3 missed heartbeats at 15s interval + buffer)
# Reaper — human-input timeout scan
REAPER_HUMAN_INPUT_TIMEOUT_QUERY = """
UPDATE tasks
SET status = 'dead_letter',
    dead_letter_reason = 'human_input_timeout',
    last_error_code = 'human_input_timeout',
    last_error_message = 'No human response within timeout period',
    dead_lettered_at = NOW(),
    pending_input_prompt = NULL,
    pending_approval_action = NULL,
    human_input_timeout_at = NULL,
    version = version + 1,
    updated_at = NOW()
WHERE status IN ('waiting_for_approval', 'waiting_for_input')
  AND human_input_timeout_at IS NOT NULL
  AND human_input_timeout_at < NOW()
RETURNING task_id, tenant_id, agent_id;
"""

STALE_WORKER_QUERY = """
UPDATE workers
SET status = 'offline'
WHERE status = 'online'
  AND last_heartbeat_at < NOW() - INTERVAL '90 seconds'
RETURNING worker_id;
"""


class ReaperTask:
    """Distributed reaper that scans for expired leases and timed-out tasks.

    Every worker runs an instance. Jittered interval prevents thundering herd.
    All operations use UPDATE ... RETURNING to avoid TOCTOU races.
    """

    def __init__(
        self,
        config: WorkerConfig,
        pool: asyncpg.Pool,
        metrics: MetricsCollector,
    ) -> None:
        self._config = config
        self._pool = pool
        self._metrics = metrics
        self._log = get_logger(config.worker_id, component="reaper")
        self._running = False
        self._task: asyncio.Task | None = None

    @property
    def running(self) -> bool:
        return self._running

    async def start(self) -> None:
        """Start the reaper loop."""
        self._running = True
        self._task = asyncio.create_task(self._reaper_loop())
        await self._log.ainfo("reaper_started")

    async def stop(self) -> None:
        """Stop the reaper loop."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        await self._log.ainfo("reaper_stopped")

    def _jittered_interval(self) -> float:
        """Return the next reaper interval with jitter.

        Base interval: reaper_interval_seconds (default 30s)
        Jitter: +/- reaper_jitter_seconds (default 10s)
        Result: 20s to 40s with default config.
        """
        base = self._config.reaper_interval_seconds
        jitter = self._config.reaper_jitter_seconds
        return base + random.uniform(-jitter, jitter)

    async def _reaper_loop(self) -> None:
        """Main reaper loop — scan at jittered intervals."""
        while self._running:
            try:
                await asyncio.sleep(self._jittered_interval())
                if not self._running:
                    break
                await self.run_once()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                await self._log.aerror("reaper_error", error=str(exc), exc_info=True)
                await asyncio.sleep(1.0)

    async def run_once(self) -> dict[str, list[str]]:
        """Execute a single reaper scan. Returns dict of actions taken.

        This method is public so tests and external code can trigger
        a reaper cycle without waiting for the jittered interval.

        Returns:
            Dict with keys 'requeued', 'dead_lettered_expired', 'dead_lettered_timeout',
            'dead_lettered_human_timeout' containing lists of task_ids.
        """
        results: dict[str, list[str]] = {
            "requeued": [],
            "dead_lettered_expired": [],
            "dead_lettered_timeout": [],
            "dead_lettered_human_timeout": [],
        }

        async with self._pool.acquire() as conn:
            async with conn.transaction():
                # (a) Expired leases — requeue
                requeued_rows = await conn.fetch(REAPER_REQUEUE_QUERY)
                for row in requeued_rows:
                    task_id = str(row["task_id"])
                    results["requeued"].append(task_id)
                    self._metrics.increment("leases.expired")
                    await self._log.ainfo(
                        REAPER_LEASE_EXPIRED,
                        task_id=task_id,
                        action="requeued",
                    )

                # (a) Expired leases — dead-letter (retries exhausted)
                dl_rows = await conn.fetch(REAPER_DEAD_LETTER_QUERY)
                for row in dl_rows:
                    task_id = str(row["task_id"])
                    results["dead_lettered_expired"].append(task_id)
                    self._metrics.increment("leases.expired")
                    self._metrics.increment("tasks.dead_letter")
                    await self._log.ainfo(
                        REAPER_DEAD_LETTERED,
                        task_id=task_id,
                        reason="retries_exhausted",
                    )

                # (b) Task timeouts
                timeout_rows = await conn.fetch(REAPER_TIMEOUT_QUERY)
                for row in timeout_rows:
                    task_id = str(row["task_id"])
                    results["dead_lettered_timeout"].append(task_id)
                    self._metrics.increment("tasks.dead_letter")
                    await self._log.ainfo(
                        REAPER_TASK_TIMEOUT,
                        task_id=task_id,
                        reason="task_timeout",
                    )

                # (c) Human-input timeouts
                human_timeout_rows = await conn.fetch(REAPER_HUMAN_INPUT_TIMEOUT_QUERY)
                for row in human_timeout_rows:
                    task_id = str(row["task_id"])
                    results["dead_lettered_human_timeout"].append(task_id)
                    self._metrics.increment("tasks.dead_letter")
                    await self._log.ainfo(
                        REAPER_DEAD_LETTERED,
                        task_id=task_id,
                        reason="human_input_timeout",
                    )

                # (d) Stale workers — mark offline if heartbeat expired
                stale_rows = await conn.fetch(STALE_WORKER_QUERY)
                for row in stale_rows:
                    worker_id = row["worker_id"]
                    self._metrics.increment("workers.stale")
                    await self._log.awarning(
                        "reaper_stale_worker",
                        worker_id=worker_id,
                        action="marked_offline",
                    )

                # Update queue depth metric
                depth_row = await conn.fetchrow(QUEUE_DEPTH_QUERY)
                if depth_row:
                    self._metrics.set_gauge("queue.depth", float(depth_row["depth"]))

        return results
