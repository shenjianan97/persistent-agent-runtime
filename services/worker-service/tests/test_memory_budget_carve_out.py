"""Budget carve-out — Phase 2 Track 5 Task 6.

The ``memory_write`` super-step MUST NOT trip ``budget_max_per_task`` pause
enforcement. The commit path writes the summarizer cost directly into
``agent_cost_ledger`` (attributed to the current checkpoint) and rolls the
cost into the agent's hourly-window accumulator, but does NOT go through
:meth:`GraphExecutor._check_budget_and_pause`.

This test verifies the behavioural invariant end-to-end by calling the commit
helper on a task whose summarizer cost alone would blow ``budget_max_per_task``
if it flowed through the per-step check. The task must still be marked
``completed`` (never ``paused``), and the hourly-window counter must reflect
the cost.
"""

from __future__ import annotations

import json
import os
import uuid

import asyncpg
import pytest

from core.config import WorkerConfig
from executor.graph import GraphExecutor


DB_DSN = os.getenv(
    "E2E_DB_DSN",
    "postgresql://postgres:postgres@localhost:55433/persistent_agent_runtime_e2e",
)

TENANT_ID = "default"
AGENT_ID = "memory-budget-test-agent"
WORKER_A = "memory-budget-worker-a"


async def _scrub(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM agent_memory_entries WHERE tenant_id = $1", TENANT_ID
        )
        await conn.execute(
            "DELETE FROM agent_cost_ledger WHERE tenant_id = $1 AND agent_id = $2",
            TENANT_ID, AGENT_ID,
        )
        await conn.execute(
            "DELETE FROM agent_runtime_state WHERE tenant_id = $1 AND agent_id = $2",
            TENANT_ID, AGENT_ID,
        )
        await conn.execute("DELETE FROM task_events WHERE tenant_id = $1", TENANT_ID)
        await conn.execute("DELETE FROM checkpoint_writes")
        await conn.execute("DELETE FROM checkpoints")
        await conn.execute(
            "DELETE FROM tasks WHERE tenant_id = $1 AND agent_id = $2",
            TENANT_ID, AGENT_ID,
        )
        await conn.execute(
            "DELETE FROM agents WHERE tenant_id = $1 AND agent_id = $2",
            TENANT_ID, AGENT_ID,
        )


@pytest.fixture
async def integration_pool():
    try:
        pool = await asyncpg.create_pool(DB_DSN, min_size=1, max_size=2)
    except Exception as exc:
        pytest.skip(f"PostgreSQL test database is not available: {exc}")

    await _scrub(pool)
    async with pool.acquire() as conn:
        # Tight per-task cap — 50 microdollars. The summarizer cost below is
        # 100 microdollars, well past the cap. A Track-3-style per-step check
        # would pause the task; the Track-5 commit path must NOT.
        await conn.execute(
            """
            INSERT INTO agents (tenant_id, agent_id, display_name, agent_config,
                                status, budget_max_per_task, budget_max_per_hour)
            VALUES ($1, $2, 'Budget Test', '{}'::jsonb, 'active', 50, 1000000)
            """,
            TENANT_ID, AGENT_ID,
        )
    try:
        yield pool
    finally:
        await _scrub(pool)
        await pool.close()


@pytest.mark.asyncio
async def test_memory_write_cost_does_not_pause_task_even_when_over_budget(
    integration_pool: asyncpg.Pool,
) -> None:
    task_id = str(uuid.uuid4())
    agent_config = {
        "model": "claude-haiku-4-5",
        "memory": {"enabled": True, "max_entries": 10_000},
    }

    async with integration_pool.acquire() as conn:
        await conn.execute(
            "UPDATE agents SET agent_config = $3::jsonb "
            "WHERE tenant_id = $1 AND agent_id = $2",
            TENANT_ID, AGENT_ID, json.dumps(agent_config),
        )
        # Seed a running task and a checkpoint (so the commit path has one
        # to attribute summarizer cost against).
        await conn.execute(
            """
            INSERT INTO tasks (task_id, tenant_id, agent_id,
                               agent_config_snapshot, status, input,
                               lease_owner, lease_expiry, version)
            VALUES ($1::uuid, $2, $3, $4::jsonb, 'running', 'input',
                    $5, NOW() + INTERVAL '60 seconds', 1)
            """,
            task_id, TENANT_ID, AGENT_ID,
            json.dumps(agent_config), WORKER_A,
        )
        await conn.execute(
            """
            INSERT INTO checkpoints (task_id, checkpoint_ns, checkpoint_id,
                                     worker_id, thread_ts, checkpoint_payload,
                                     metadata_payload)
            VALUES ($1::uuid, '', 'cp-budget', $2, '2026-04-17T00:00:00Z',
                    '{}'::jsonb, '{}'::jsonb)
            """,
            task_id, WORKER_A,
        )

    executor = GraphExecutor(WorkerConfig(worker_id=WORKER_A), integration_pool)

    pending_memory = {
        "title": "Expensive memory",
        "summary": "The summarizer burned 100 microdollars",
        "outcome": "succeeded",
        "content_vec": None,
        "summarizer_model_id": "claude-haiku-4-5",
        "observations_snapshot": [],
        "tags": [],
        "summarizer_tokens_in": 1000,
        "summarizer_tokens_out": 500,
        # 100 microdollars — 2× the per-task cap.
        "summarizer_cost_microdollars": 100,
        "embedding_tokens": 0,
        "embedding_cost_microdollars": 0,
    }

    result = await executor._commit_memory_and_complete_task(
        task_id=task_id,
        tenant_id=TENANT_ID,
        agent_id=AGENT_ID,
        pending_memory=pending_memory,
        agent_config=agent_config,
        output={"result": "done"},
        worker_id=WORKER_A,
    )
    assert result["committed"] is True

    async with integration_pool.acquire() as conn:
        task_row = await conn.fetchrow(
            "SELECT status, pause_reason FROM tasks WHERE task_id = $1::uuid",
            task_id,
        )
        ledger = await conn.fetch(
            "SELECT cost_microdollars, checkpoint_id FROM agent_cost_ledger "
            "WHERE task_id = $1::uuid ORDER BY created_at",
            task_id,
        )
        hour_cost = await conn.fetchval(
            "SELECT hour_window_cost_microdollars FROM agent_runtime_state "
            "WHERE tenant_id = $1 AND agent_id = $2",
            TENANT_ID, AGENT_ID,
        )

    # Invariant #1: the task is completed. It is NOT paused despite the
    # summarizer cost being 2× the per-task cap.
    assert task_row["status"] == "completed"
    assert task_row["pause_reason"] is None

    # Invariant #2: the summarizer cost IS written to the ledger attributed
    # to the task's checkpoint.
    ledger_by_cp = {row["checkpoint_id"]: row["cost_microdollars"] for row in ledger}
    assert ledger_by_cp.get("cp-budget") == 100

    # Invariant #3: hourly spend still accrued the full cost.
    assert int(hour_cost or 0) == 100
