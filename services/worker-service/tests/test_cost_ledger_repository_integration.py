"""Integration tests for ``core/cost_ledger_repository.py``.

Narrow coverage — only invariants not exercised by
``test_cost_ledger_integration.py`` or the memory-write / budget tests:

- :func:`sum_hourly_cost_for_agent` must exclude entries older than
  60 minutes and must scope by ``(tenant_id, agent_id)``. Backdated rows
  are hard to set up from higher-level paths, so the boundary is easy to
  regress without a targeted assertion here.
- :func:`min_created_at_in_hour_window` must return the earliest entry
  inside the window — drives the budget-pause resume-time estimate.

Runs against the isolated test DB on port 55433 (``make worker-test``).
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import asyncpg
import pytest

from core.cost_ledger_repository import (
    min_created_at_in_hour_window,
    sum_hourly_cost_for_agent,
)


DB_DSN = os.getenv(
    "E2E_DB_DSN",
    "postgresql://postgres:postgres@localhost:55433/persistent_agent_runtime_e2e",
)

TENANT_ID = "default"
AGENT_ID = "cost-ledger-repo-test-agent"
OTHER_AGENT_ID = "cost-ledger-repo-other-agent"
WORKER_ID = "worker-a"


@pytest.fixture
async def integration_pool():
    try:
        pool = await asyncpg.create_pool(DB_DSN, min_size=1, max_size=2)
    except Exception as exc:
        pytest.skip(f"PostgreSQL test database is not available: {exc}")

    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM agent_cost_ledger WHERE tenant_id = $1 AND agent_id = ANY($2::text[])",
            TENANT_ID, [AGENT_ID, OTHER_AGENT_ID],
        )
        await conn.execute(
            "DELETE FROM tasks WHERE tenant_id = $1 AND agent_id = ANY($2::text[])",
            TENANT_ID, [AGENT_ID, OTHER_AGENT_ID],
        )
        await conn.execute(
            "DELETE FROM agents WHERE tenant_id = $1 AND agent_id = ANY($2::text[])",
            TENANT_ID, [AGENT_ID, OTHER_AGENT_ID],
        )
        for aid in (AGENT_ID, OTHER_AGENT_ID):
            await conn.execute(
                """
                INSERT INTO agents (tenant_id, agent_id, display_name, agent_config, status)
                VALUES ($1, $2, 'Cost Ledger Repo Test Agent', '{}'::jsonb, 'active')
                """,
                TENANT_ID, aid,
            )

    try:
        yield pool
    finally:
        async with pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM agent_cost_ledger WHERE tenant_id = $1 AND agent_id = ANY($2::text[])",
                TENANT_ID, [AGENT_ID, OTHER_AGENT_ID],
            )
            await conn.execute(
                "DELETE FROM tasks WHERE tenant_id = $1 AND agent_id = ANY($2::text[])",
                TENANT_ID, [AGENT_ID, OTHER_AGENT_ID],
            )
            await conn.execute(
                "DELETE FROM agents WHERE tenant_id = $1 AND agent_id = ANY($2::text[])",
                TENANT_ID, [AGENT_ID, OTHER_AGENT_ID],
            )
        await pool.close()


async def _seed_task(pool: asyncpg.Pool, *, agent_id: str = AGENT_ID) -> str:
    task_id = str(uuid.uuid4())
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO tasks (
                task_id, tenant_id, agent_id, agent_config_snapshot,
                status, input, lease_owner, lease_expiry, version
            ) VALUES ($1::uuid, $2, $3, '{}'::jsonb, 'running', 'input', $4,
                      NOW() + INTERVAL '60 seconds', 1)
            """,
            task_id, TENANT_ID, agent_id, WORKER_ID,
        )
    return task_id


async def _insert_ledger_row(
    pool: asyncpg.Pool,
    *,
    task_id: str,
    checkpoint_id: str,
    cost_microdollars: int,
    created_at: datetime | None = None,
    agent_id: str = AGENT_ID,
) -> None:
    async with pool.acquire() as conn:
        if created_at is None:
            await conn.execute(
                """
                INSERT INTO agent_cost_ledger
                    (tenant_id, agent_id, task_id, checkpoint_id, cost_microdollars)
                VALUES ($1, $2, $3::uuid, $4, $5)
                """,
                TENANT_ID, agent_id, task_id, checkpoint_id, cost_microdollars,
            )
        else:
            await conn.execute(
                """
                INSERT INTO agent_cost_ledger
                    (tenant_id, agent_id, task_id, checkpoint_id,
                     cost_microdollars, created_at)
                VALUES ($1, $2, $3::uuid, $4, $5, $6)
                """,
                TENANT_ID, agent_id, task_id, checkpoint_id, cost_microdollars,
                created_at,
            )


@pytest.mark.asyncio
async def test_sum_hourly_cost_for_agent_excludes_rows_older_than_60_minutes(
    integration_pool: asyncpg.Pool,
) -> None:
    task_id = await _seed_task(integration_pool)
    now = datetime.now(timezone.utc)
    await _insert_ledger_row(
        integration_pool, task_id=task_id, checkpoint_id="ckpt-stale",
        cost_microdollars=10_000,
        created_at=now - timedelta(minutes=90),
    )
    await _insert_ledger_row(
        integration_pool, task_id=task_id, checkpoint_id="ckpt-recent",
        cost_microdollars=200,
        created_at=now - timedelta(minutes=15),
    )
    await _insert_ledger_row(
        integration_pool, task_id=task_id, checkpoint_id="ckpt-now",
        cost_microdollars=50,
    )

    async with integration_pool.acquire() as conn:
        total = await sum_hourly_cost_for_agent(conn, TENANT_ID, AGENT_ID)
    assert total == 250


@pytest.mark.asyncio
async def test_sum_hourly_cost_for_agent_scopes_to_tenant_and_agent(
    integration_pool: asyncpg.Pool,
) -> None:
    task_mine = await _seed_task(integration_pool, agent_id=AGENT_ID)
    task_other = await _seed_task(integration_pool, agent_id=OTHER_AGENT_ID)
    await _insert_ledger_row(
        integration_pool, task_id=task_mine, checkpoint_id="ckpt",
        cost_microdollars=100,
    )
    await _insert_ledger_row(
        integration_pool, task_id=task_other, checkpoint_id="ckpt",
        cost_microdollars=9_000, agent_id=OTHER_AGENT_ID,
    )

    async with integration_pool.acquire() as conn:
        total = await sum_hourly_cost_for_agent(conn, TENANT_ID, AGENT_ID)
    assert total == 100


@pytest.mark.asyncio
async def test_min_created_at_in_hour_window_returns_earliest_inside_window(
    integration_pool: asyncpg.Pool,
) -> None:
    task_id = await _seed_task(integration_pool)
    now = datetime.now(timezone.utc)
    # Outside the window — must be ignored.
    await _insert_ledger_row(
        integration_pool, task_id=task_id, checkpoint_id="ckpt-stale",
        cost_microdollars=1,
        created_at=now - timedelta(minutes=90),
    )
    in_window_earlier = now - timedelta(minutes=45)
    in_window_later = now - timedelta(minutes=10)
    await _insert_ledger_row(
        integration_pool, task_id=task_id, checkpoint_id="ckpt-later",
        cost_microdollars=1, created_at=in_window_later,
    )
    await _insert_ledger_row(
        integration_pool, task_id=task_id, checkpoint_id="ckpt-earlier",
        cost_microdollars=1, created_at=in_window_earlier,
    )

    async with integration_pool.acquire() as conn:
        earliest = await min_created_at_in_hour_window(
            conn, TENANT_ID, AGENT_ID
        )

    assert earliest is not None
    # Allow sub-second tolerance on clock skew between test and DB.
    assert abs((earliest - in_window_earlier).total_seconds()) < 1
