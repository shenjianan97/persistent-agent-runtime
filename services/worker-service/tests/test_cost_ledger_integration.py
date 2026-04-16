"""Integration tests for lease-CAS gating of cost-ledger writes.

Covers the race where a worker's heartbeat has missed and its lease was
stripped, but it is still mid-flight recording step cost. Without a lease
check the evicted worker would charge tokens for a task it no longer owns.
"""

from __future__ import annotations

import json
import os
import uuid

import asyncpg
import pytest

from checkpointer.postgres import LeaseRevokedException
from core.config import WorkerConfig
from executor.graph import GraphExecutor


DB_DSN = os.getenv(
    "E2E_DB_DSN",
    "postgresql://postgres:postgres@localhost:55433/persistent_agent_runtime_e2e",
)

TENANT_ID = "default"
AGENT_ID = "cost-ledger-test-agent"
WORKER_A = "worker-a"
WORKER_B = "worker-b"


@pytest.fixture
async def integration_pool():
    try:
        pool = await asyncpg.create_pool(DB_DSN, min_size=1, max_size=2)
    except Exception as exc:
        pytest.skip(f"PostgreSQL test database is not available: {exc}")

    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM agent_cost_ledger")
        await conn.execute("DELETE FROM agent_runtime_state")
        await conn.execute("DELETE FROM task_events")
        await conn.execute("DELETE FROM checkpoint_writes")
        await conn.execute("DELETE FROM checkpoints")
        await conn.execute("DELETE FROM tasks")
        await conn.execute("DELETE FROM agents")

    try:
        yield pool
    finally:
        await pool.close()


async def _seed_task_with_checkpoint(
    pool: asyncpg.Pool,
    *,
    task_id: str,
    checkpoint_id: str,
    lease_owner: str | None = WORKER_A,
) -> None:
    agent_config = {
        "system_prompt": "test",
        "model": "claude-sonnet-4-6",
        "temperature": 0.1,
        "allowed_tools": [],
    }
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO agents (tenant_id, agent_id, display_name, agent_config, status)
            VALUES ($1, $2, 'Test Agent', $3::jsonb, 'active')
            ON CONFLICT (tenant_id, agent_id) DO NOTHING
            """,
            TENANT_ID, AGENT_ID, json.dumps(agent_config),
        )
        await conn.execute(
            """
            INSERT INTO tasks (
                task_id, tenant_id, agent_id, agent_config_snapshot,
                status, input, lease_owner, lease_expiry, version
            ) VALUES ($1::uuid, $2, $3, $4::jsonb, 'running', 'input', $5,
                      NOW() + INTERVAL '60 seconds', 1)
            """,
            task_id, TENANT_ID, AGENT_ID, json.dumps(agent_config), lease_owner,
        )
        await conn.execute(
            """
            INSERT INTO checkpoints (
                task_id, checkpoint_ns, checkpoint_id, worker_id, thread_ts,
                checkpoint_payload, metadata_payload
            ) VALUES ($1::uuid, '', $2, $3, '2026-04-16T00:00:00Z',
                      '{}'::jsonb, '{}'::jsonb)
            """,
            task_id, checkpoint_id, WORKER_A,
        )


def _make_executor(pool: asyncpg.Pool) -> GraphExecutor:
    config = WorkerConfig(worker_id=WORKER_A)
    return GraphExecutor(config, pool)


@pytest.mark.asyncio
async def test_record_step_cost_succeeds_when_lease_is_held(
    integration_pool: asyncpg.Pool,
) -> None:
    task_id = str(uuid.uuid4())
    checkpoint_id = "cp-happy"
    await _seed_task_with_checkpoint(
        integration_pool, task_id=task_id, checkpoint_id=checkpoint_id
    )

    executor = _make_executor(integration_pool)
    async with integration_pool.acquire() as conn:
        async with conn.transaction():
            cumulative, hourly = await executor._record_step_cost(
                conn, task_id, TENANT_ID, AGENT_ID, checkpoint_id, 1000,
                execution_metadata={"input_tokens": 10, "output_tokens": 5},
                worker_id=WORKER_A,
            )

    assert cumulative == 1000
    assert hourly == 1000

    async with integration_pool.acquire() as conn:
        ledger_rows = await conn.fetch(
            "SELECT cost_microdollars FROM agent_cost_ledger WHERE task_id=$1::uuid",
            task_id,
        )
        cp_cost = await conn.fetchval(
            "SELECT cost_microdollars FROM checkpoints WHERE task_id=$1::uuid",
            task_id,
        )

    assert [r["cost_microdollars"] for r in ledger_rows] == [1000]
    assert cp_cost == 1000


@pytest.mark.asyncio
async def test_record_step_cost_rejects_when_lease_revoked(
    integration_pool: asyncpg.Pool,
) -> None:
    task_id = str(uuid.uuid4())
    checkpoint_id = "cp-revoked"
    await _seed_task_with_checkpoint(
        integration_pool, task_id=task_id, checkpoint_id=checkpoint_id
    )

    async with integration_pool.acquire() as conn:
        await conn.execute(
            "UPDATE tasks SET lease_owner = NULL WHERE task_id = $1::uuid",
            task_id,
        )

    executor = _make_executor(integration_pool)
    with pytest.raises(LeaseRevokedException):
        async with integration_pool.acquire() as conn:
            async with conn.transaction():
                await executor._record_step_cost(
                    conn, task_id, TENANT_ID, AGENT_ID, checkpoint_id, 1000,
                    execution_metadata={"input_tokens": 10, "output_tokens": 5},
                    worker_id=WORKER_A,
                )

    async with integration_pool.acquire() as conn:
        ledger_count = await conn.fetchval(
            "SELECT COUNT(*) FROM agent_cost_ledger WHERE task_id=$1::uuid",
            task_id,
        )
        cp_cost = await conn.fetchval(
            "SELECT cost_microdollars FROM checkpoints WHERE task_id=$1::uuid",
            task_id,
        )
        hourly = await conn.fetchval(
            "SELECT hour_window_cost_microdollars FROM agent_runtime_state "
            "WHERE tenant_id=$1 AND agent_id=$2",
            TENANT_ID, AGENT_ID,
        )

    assert ledger_count == 0, "evicted worker must not insert cost ledger rows"
    assert cp_cost == 0, "evicted worker must not update checkpoint cost"
    assert hourly in (None, 0), "evicted worker must not bump agent_runtime_state"


@pytest.mark.asyncio
async def test_record_step_cost_rejects_when_lease_reassigned(
    integration_pool: asyncpg.Pool,
) -> None:
    task_id = str(uuid.uuid4())
    checkpoint_id = "cp-reassigned"
    await _seed_task_with_checkpoint(
        integration_pool, task_id=task_id, checkpoint_id=checkpoint_id
    )

    async with integration_pool.acquire() as conn:
        await conn.execute(
            "UPDATE tasks SET lease_owner = $1 WHERE task_id = $2::uuid",
            WORKER_B, task_id,
        )

    executor = _make_executor(integration_pool)
    with pytest.raises(LeaseRevokedException):
        async with integration_pool.acquire() as conn:
            async with conn.transaction():
                await executor._record_step_cost(
                    conn, task_id, TENANT_ID, AGENT_ID, checkpoint_id, 1000,
                    execution_metadata={"input_tokens": 10, "output_tokens": 5},
                    worker_id=WORKER_A,
                )

    async with integration_pool.acquire() as conn:
        ledger_count = await conn.fetchval(
            "SELECT COUNT(*) FROM agent_cost_ledger WHERE task_id=$1::uuid",
            task_id,
        )

    assert ledger_count == 0
