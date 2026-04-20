"""Distributed Reaper — reclaims expired leases and timed-out tasks.

Runs on every worker instance at a jittered interval (30s +/- 10s).
Handles multiple conditions:
  (a) Expired leases: requeue with retry_count++ or dead-letter if exhausted.
  (b) Task timeouts: dead-letter with reason 'task_timeout'.
  (c) Human-input timeouts: dead-letter with reason 'human_input_timeout'.
  (d) Track 3: Hourly budget auto-recovery, running-count reconciliation, cost ledger pruning.
Both requeue paths emit pg_notify('new_task', worker_pool_id) in the same txn.
"""

from __future__ import annotations

import asyncio
import json
import random

import asyncpg

from core.agent_runtime_state_repository import decrement_running_count
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
    RETURNING task_id, tenant_id, agent_id, worker_pool_id
)
, notified AS (
    SELECT pg_notify('new_task', worker_pool_id)
    FROM requeued
)
SELECT task_id, tenant_id, agent_id
FROM requeued;
"""

# Reaper — expired leases, dead-letter (retry_count >= max_retries)
#
# ``human_response`` is cleared alongside the status flip to keep dead-letter
# semantics aligned with the worker's own ``_handle_dead_letter`` path — a
# subsequent redrive must not re-inject a pending follow-up / input payload
# whose HumanMessage is already persisted in state["messages"] via the
# pre-crash checkpoint.
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
    human_response = NULL,
    version = version + 1,
    updated_at = NOW()
WHERE status = 'running'
  AND lease_expiry < NOW()
  AND retry_count >= max_retries
RETURNING task_id, tenant_id, agent_id;
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
    human_response = NULL,
    version = version + 1,
    updated_at = NOW()
WHERE status IN ('running', 'queued')
  AND timeout_reference_at + (task_timeout_seconds * INTERVAL '1 second') < NOW()
RETURNING task_id, tenant_id, agent_id;
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
                    # Track 3: Decrement running_task_count on requeue
                    await decrement_running_count(
                        conn, row["tenant_id"], row["agent_id"]
                    )
                    await _insert_task_event(
                        conn, task_id, row["tenant_id"], row["agent_id"],
                        "task_reclaimed_after_lease_expiry", "running", "queued",
                    )
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
                    # Track 3: Decrement running_task_count on dead-letter
                    await decrement_running_count(
                        conn, row["tenant_id"], row["agent_id"]
                    )
                    await _insert_task_event(
                        conn, task_id, row["tenant_id"], row["agent_id"],
                        "task_dead_lettered", "running", "dead_letter",
                        error_code="retries_exhausted",
                    )
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
                    # Track 3: Decrement running_task_count on timeout
                    await decrement_running_count(
                        conn, row["tenant_id"], row["agent_id"]
                    )
                    await _insert_task_event(
                        conn, task_id, row["tenant_id"], row["agent_id"],
                        "task_dead_lettered", None, "dead_letter",
                        error_code="task_timeout",
                    )
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
                    await _insert_task_event(
                        conn, task_id, row["tenant_id"], row["agent_id"],
                        "task_dead_lettered", None, "dead_letter",
                        error_code="human_input_timeout",
                    )
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

        # Track 3: Scheduler and Budget scans (independent — failure in one doesn't prevent others)
        try:
            recovered = await self._recover_hourly_budget_pauses()
            if recovered:
                results['budget_recovered'] = recovered
                await self._log.ainfo("reaper_budget_recovery", count=len(recovered))
        except Exception as exc:
            await self._log.aerror("reaper_budget_recovery_error", error=str(exc), exc_info=True)

        try:
            corrections = await self._reconcile_runtime_state()
            if corrections:
                await self._log.ainfo("reaper_reconciliation", corrections=corrections)
        except Exception as exc:
            await self._log.aerror("reaper_reconciliation_error", error=str(exc), exc_info=True)

        try:
            pruned = await self._prune_cost_ledger()
            if pruned > 0:
                await self._log.ainfo("reaper_ledger_pruned", count=pruned)
        except Exception as exc:
            await self._log.aerror("reaper_ledger_prune_error", error=str(exc), exc_info=True)

        return results

    async def _recover_hourly_budget_pauses(self) -> list[str]:
        """Resume tasks paused for hourly budget once the rolling window clears."""
        recovered_task_ids: list[str] = []

        # Find agents with hourly-budget-paused tasks whose resume_eligible_at has passed
        paused_agents = await self._pool.fetch(
            '''SELECT DISTINCT t.tenant_id, t.agent_id
               FROM tasks t
               WHERE t.status = 'paused'
                 AND t.pause_reason = 'budget_per_hour'
                 AND t.resume_eligible_at IS NOT NULL
                 AND t.resume_eligible_at <= NOW()'''
        )

        for agent_row in paused_agents:
            tenant_id = agent_row['tenant_id']
            agent_id = agent_row['agent_id']

            async with self._pool.acquire() as conn:
                async with conn.transaction():
                    # Verify agent is active
                    agent = await conn.fetchrow(
                        '''SELECT budget_max_per_hour, max_concurrent_tasks, status
                           FROM agents WHERE tenant_id = $1 AND agent_id = $2''',
                        tenant_id, agent_id
                    )
                    if not agent or agent['status'] != 'active':
                        continue

                    # Recompute rolling hourly spend from canonical ledger
                    hour_cost = await conn.fetchval(
                        '''SELECT COALESCE(SUM(cost_microdollars), 0)
                           FROM agent_cost_ledger
                           WHERE tenant_id = $1 AND agent_id = $2
                             AND created_at > NOW() - INTERVAL '60 minutes' ''',
                        tenant_id, agent_id
                    )

                    if hour_cost >= agent['budget_max_per_hour']:
                        continue  # Still over budget

                    # Get current running count
                    runtime = await conn.fetchrow(
                        '''SELECT running_task_count FROM agent_runtime_state
                           WHERE tenant_id = $1 AND agent_id = $2
                           FOR UPDATE''',
                        tenant_id, agent_id
                    )
                    current_running = runtime['running_task_count'] if runtime else 0
                    available_slots = agent['max_concurrent_tasks'] - current_running

                    if available_slots <= 0:
                        continue  # No concurrency slots

                    # Resume up to available_slots tasks
                    resumed_rows = await conn.fetch(
                        '''UPDATE tasks
                           SET status = 'queued',
                               pause_reason = NULL,
                               pause_details = NULL,
                               resume_eligible_at = NULL,
                               version = version + 1,
                               updated_at = NOW()
                           WHERE task_id IN (
                               SELECT task_id FROM tasks
                               WHERE tenant_id = $1 AND agent_id = $2
                                 AND status = 'paused'
                                 AND pause_reason = 'budget_per_hour'
                               ORDER BY created_at ASC
                               LIMIT $3
                               FOR UPDATE SKIP LOCKED
                           )
                           RETURNING task_id, worker_pool_id''',
                        tenant_id, agent_id, available_slots
                    )

                    # Record events and notify for each resumed task
                    notified_pools: set[str] = set()
                    for row in resumed_rows:
                        await _insert_task_event(
                            conn, str(row['task_id']), tenant_id, agent_id,
                            event_type='task_resumed',
                            status_before='paused',
                            status_after='queued',
                            worker_id=None,
                            details={
                                'resume_trigger': 'automatic_hourly_recovery',
                                'agent_hour_cost_at_resume': int(hour_cost),
                                'budget_max_per_hour': int(agent['budget_max_per_hour'])
                            }
                        )
                        notified_pools.add(row['worker_pool_id'])
                        recovered_task_ids.append(str(row['task_id']))

                    # Notify all affected worker pools
                    for pool_id in notified_pools:
                        await conn.execute(
                            "SELECT pg_notify('new_task', $1)", pool_id
                        )

                    # Update hourly cost cache while we have the accurate value
                    await conn.execute(
                        '''UPDATE agent_runtime_state
                           SET hour_window_cost_microdollars = $1, updated_at = NOW()
                           WHERE tenant_id = $2 AND agent_id = $3''',
                        hour_cost, tenant_id, agent_id
                    )

        return recovered_task_ids

    async def _reconcile_runtime_state(self) -> str:
        """Reconcile agent_runtime_state with actual task counts and ledger spend."""
        # Reconcile running_task_count from actual running tasks
        count_corrections = await self._pool.execute(
            '''UPDATE agent_runtime_state ars
               SET running_task_count = sub.actual_count, updated_at = NOW()
               FROM (
                   SELECT tenant_id, agent_id, COUNT(*) AS actual_count
                   FROM tasks WHERE status = 'running'
                   GROUP BY tenant_id, agent_id
               ) sub
               WHERE ars.tenant_id = sub.tenant_id AND ars.agent_id = sub.agent_id
                 AND ars.running_task_count != sub.actual_count'''
        )

        # Zero out counts for agents with no running tasks but non-zero count
        await self._pool.execute(
            '''UPDATE agent_runtime_state ars
               SET running_task_count = 0, updated_at = NOW()
               WHERE ars.running_task_count > 0
                 AND NOT EXISTS (
                     SELECT 1 FROM tasks t
                     WHERE t.tenant_id = ars.tenant_id
                       AND t.agent_id = ars.agent_id
                       AND t.status = 'running'
                 )'''
        )

        # Reconcile hour_window_cost_microdollars from canonical ledger
        await self._pool.execute(
            '''UPDATE agent_runtime_state ars
               SET hour_window_cost_microdollars = COALESCE(sub.actual_cost, 0), updated_at = NOW()
               FROM (
                   SELECT tenant_id, agent_id, SUM(cost_microdollars) AS actual_cost
                   FROM agent_cost_ledger
                   WHERE created_at > NOW() - INTERVAL '60 minutes'
                   GROUP BY tenant_id, agent_id
               ) sub
               WHERE ars.tenant_id = sub.tenant_id AND ars.agent_id = sub.agent_id
                 AND ars.hour_window_cost_microdollars != COALESCE(sub.actual_cost, 0)'''
        )

        # Zero out hourly cost for agents with no recent ledger entries
        await self._pool.execute(
            '''UPDATE agent_runtime_state ars
               SET hour_window_cost_microdollars = 0, updated_at = NOW()
               WHERE ars.hour_window_cost_microdollars > 0
                 AND NOT EXISTS (
                     SELECT 1 FROM agent_cost_ledger acl
                     WHERE acl.tenant_id = ars.tenant_id
                       AND acl.agent_id = ars.agent_id
                       AND acl.created_at > NOW() - INTERVAL '60 minutes'
                 )'''
        )

        return count_corrections

    async def _prune_cost_ledger(self) -> int:
        """Delete agent_cost_ledger entries older than 2 hours."""
        result = await self._pool.execute(
            '''DELETE FROM agent_cost_ledger
               WHERE created_at < NOW() - INTERVAL '2 hours' '''
        )
        # asyncpg execute returns a string like 'DELETE 5'
        try:
            return int(result.split()[-1]) if result else 0
        except (ValueError, IndexError):
            return 0


async def _insert_task_event(
    conn,
    task_id: str,
    tenant_id: str,
    agent_id: str,
    event_type: str,
    status_before: str | None,
    status_after: str | None,
    worker_id: str | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
    details: dict | None = None,
):
    """Insert a task event on the current transaction-scoped connection.

    Must be called inside an active transaction so the event INSERT commits
    or rolls back atomically with the paired task-state mutation.
    """
    await conn.execute(
        '''INSERT INTO task_events (tenant_id, task_id, agent_id, event_type,
                                    status_before, status_after, worker_id,
                                    error_code, error_message, details)
           VALUES ($1, $2::uuid, $3, $4, $5, $6, $7, $8, $9, $10::jsonb)''',
        tenant_id, task_id, agent_id, event_type,
        status_before, status_after, worker_id,
        error_code, error_message, json.dumps(details or {}),
    )
