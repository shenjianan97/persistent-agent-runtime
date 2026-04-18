"""Integration tests for ``core/agent_runtime_state_repository.py``.

Narrow coverage — only invariants not already exercised by ``test_reaper.py``
and ``test_cost_ledger_integration.py``:

- The ``'1970-01-01T00:00:00Z'`` scheduler-cursor sentinel that first-writes
  materialize. Regresses silently if someone changes the insert row.
- The ``GREATEST(running_task_count - 1, 0)`` floor under repeated decrements
  (idempotent reconciliation).

Runs against the isolated test DB on port 55433 (``make worker-test``).
"""

from __future__ import annotations

import os

import asyncpg
import pytest

from core.agent_runtime_state_repository import (
    decrement_running_count,
    increment_hour_window_cost,
)


DB_DSN = os.getenv(
    "E2E_DB_DSN",
    "postgresql://postgres:postgres@localhost:55433/persistent_agent_runtime_e2e",
)

TENANT_ID = "default"
AGENT_ID = "agent-runtime-state-repo-test-agent"


@pytest.fixture
async def integration_pool():
    try:
        pool = await asyncpg.create_pool(DB_DSN, min_size=1, max_size=2)
    except Exception as exc:
        pytest.skip(f"PostgreSQL test database is not available: {exc}")

    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM agent_runtime_state WHERE tenant_id = $1 AND agent_id = $2",
            TENANT_ID, AGENT_ID,
        )

    try:
        yield pool
    finally:
        async with pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM agent_runtime_state WHERE tenant_id = $1 AND agent_id = $2",
                TENANT_ID, AGENT_ID,
            )
        await pool.close()


async def _state_row(pool: asyncpg.Pool) -> asyncpg.Record | None:
    async with pool.acquire() as conn:
        return await conn.fetchrow(
            """
            SELECT running_task_count, hour_window_cost_microdollars,
                   scheduler_cursor
            FROM agent_runtime_state
            WHERE tenant_id = $1 AND agent_id = $2
            """,
            TENANT_ID, AGENT_ID,
        )


@pytest.mark.asyncio
async def test_first_write_uses_scheduler_cursor_sentinel(
    integration_pool: asyncpg.Pool,
) -> None:
    async with integration_pool.acquire() as conn:
        async with conn.transaction():
            await increment_hour_window_cost(conn, TENANT_ID, AGENT_ID, 1)

    row = await _state_row(integration_pool)
    # The sentinel is the epoch so the scheduler knows this is a fresh row
    # that hasn't been assigned real cursor time yet.
    assert row is not None
    assert row["scheduler_cursor"].year == 1970


@pytest.mark.asyncio
async def test_decrement_floors_at_zero_under_repeated_calls(
    integration_pool: asyncpg.Pool,
) -> None:
    async with integration_pool.acquire() as conn:
        async with conn.transaction():
            for _ in range(5):
                await decrement_running_count(conn, TENANT_ID, AGENT_ID)

    row = await _state_row(integration_pool)
    assert row["running_task_count"] == 0
