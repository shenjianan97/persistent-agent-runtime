"""Integration tests for ``core/checkpoint_repository.py``.

Narrow coverage — only behavior not exercised by existing tests:

- The distinction between :func:`fetch_latest_checkpoint_id` (no ns filter,
  streaming path) and :func:`fetch_latest_terminal_checkpoint_id`
  (``checkpoint_ns=''``, commit path). Regresses if someone changes one
  without the other.
- :func:`set_cost_and_metadata` writing ``NULL`` when metadata is ``None``.
- :func:`add_cost_and_preserve_metadata` — the COALESCE-preserves invariant
  that keeps sandbox + memory-write cost attributions from clobbering each
  other on the same checkpoint.
- :func:`add_cost_to_latest_terminal_checkpoint` — the embedded subquery
  must resolve to the main-graph checkpoint, not the newest-overall one.

Runs against the isolated test DB on port 55433 (``make worker-test``).
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid

import asyncpg
import pytest

from core.checkpoint_repository import (
    add_cost_and_preserve_metadata,
    add_cost_to_latest_terminal_checkpoint,
    fetch_latest_checkpoint_id,
    fetch_latest_terminal_checkpoint_id,
    set_cost_and_metadata,
)


DB_DSN = os.getenv(
    "E2E_DB_DSN",
    "postgresql://postgres:postgres@localhost:55433/persistent_agent_runtime_e2e",
)

TENANT_ID = "default"
AGENT_ID = "checkpoint-repo-test-agent"
WORKER_ID = "worker-a"


@pytest.fixture
async def integration_pool():
    try:
        pool = await asyncpg.create_pool(DB_DSN, min_size=1, max_size=2)
    except Exception as exc:
        pytest.skip(f"PostgreSQL test database is not available: {exc}")

    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM checkpoints WHERE task_id IN ("
            "SELECT task_id FROM tasks WHERE tenant_id = $1 AND agent_id = $2)",
            TENANT_ID, AGENT_ID,
        )
        await conn.execute(
            "DELETE FROM tasks WHERE tenant_id = $1 AND agent_id = $2",
            TENANT_ID, AGENT_ID,
        )
        await conn.execute(
            "DELETE FROM agents WHERE tenant_id = $1 AND agent_id = $2",
            TENANT_ID, AGENT_ID,
        )
        await conn.execute(
            """
            INSERT INTO agents (tenant_id, agent_id, display_name, agent_config, status)
            VALUES ($1, $2, 'Checkpoint Repo Test Agent', '{}'::jsonb, 'active')
            """,
            TENANT_ID, AGENT_ID,
        )

    try:
        yield pool
    finally:
        async with pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM checkpoints WHERE task_id IN ("
                "SELECT task_id FROM tasks WHERE tenant_id = $1 AND agent_id = $2)",
                TENANT_ID, AGENT_ID,
            )
            await conn.execute(
                "DELETE FROM tasks WHERE tenant_id = $1 AND agent_id = $2",
                TENANT_ID, AGENT_ID,
            )
            await conn.execute(
                "DELETE FROM agents WHERE tenant_id = $1 AND agent_id = $2",
                TENANT_ID, AGENT_ID,
            )
        await pool.close()


async def _seed_task(pool: asyncpg.Pool) -> str:
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
            task_id, TENANT_ID, AGENT_ID, WORKER_ID,
        )
    return task_id


async def _insert_checkpoint(
    pool: asyncpg.Pool,
    *,
    task_id: str,
    checkpoint_id: str,
    checkpoint_ns: str = "",
    thread_ts: str = "2026-04-16T00:00:00Z",
    cost_microdollars: int = 0,
    execution_metadata: dict | None = None,
) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO checkpoints (
                task_id, checkpoint_ns, checkpoint_id, worker_id, thread_ts,
                checkpoint_payload, metadata_payload, cost_microdollars,
                execution_metadata
            ) VALUES ($1::uuid, $2, $3, $4, $5, '{}'::jsonb, '{}'::jsonb, $6, $7::jsonb)
            """,
            task_id, checkpoint_ns, checkpoint_id, WORKER_ID, thread_ts,
            cost_microdollars,
            json.dumps(execution_metadata) if execution_metadata is not None else None,
        )


async def _read_checkpoint(
    pool: asyncpg.Pool, *, task_id: str, checkpoint_id: str
) -> asyncpg.Record:
    async with pool.acquire() as conn:
        return await conn.fetchrow(
            """
            SELECT cost_microdollars, execution_metadata
            FROM checkpoints
            WHERE task_id = $1::uuid AND checkpoint_id = $2
            """,
            task_id, checkpoint_id,
        )


@pytest.mark.asyncio
async def test_fetch_latest_checkpoint_id_returns_newest_regardless_of_ns(
    integration_pool: asyncpg.Pool,
) -> None:
    task_id = await _seed_task(integration_pool)
    await _insert_checkpoint(
        integration_pool, task_id=task_id, checkpoint_id="ckpt-main-old",
        checkpoint_ns="", thread_ts="2026-04-16T00:00:00Z",
    )
    await asyncio.sleep(0.01)
    await _insert_checkpoint(
        integration_pool, task_id=task_id, checkpoint_id="ckpt-sub-newer",
        checkpoint_ns="tools", thread_ts="2026-04-16T00:00:01Z",
    )

    async with integration_pool.acquire() as conn:
        assert await fetch_latest_checkpoint_id(conn, task_id) == "ckpt-sub-newer"


@pytest.mark.asyncio
async def test_fetch_latest_terminal_checkpoint_id_ignores_non_empty_ns(
    integration_pool: asyncpg.Pool,
) -> None:
    task_id = await _seed_task(integration_pool)
    await _insert_checkpoint(
        integration_pool, task_id=task_id, checkpoint_id="ckpt-main",
        checkpoint_ns="", thread_ts="2026-04-16T00:00:00Z",
    )
    await asyncio.sleep(0.01)
    await _insert_checkpoint(
        integration_pool, task_id=task_id, checkpoint_id="ckpt-sub-newer",
        checkpoint_ns="tools", thread_ts="2026-04-16T00:00:01Z",
    )

    async with integration_pool.acquire() as conn:
        assert await fetch_latest_terminal_checkpoint_id(conn, task_id) == "ckpt-main"


@pytest.mark.asyncio
async def test_set_cost_and_metadata_clears_metadata_on_none(
    integration_pool: asyncpg.Pool,
) -> None:
    task_id = await _seed_task(integration_pool)
    await _insert_checkpoint(
        integration_pool, task_id=task_id, checkpoint_id="ckpt",
        cost_microdollars=5, execution_metadata={"some": "data"},
    )

    async with integration_pool.acquire() as conn:
        async with conn.transaction():
            await set_cost_and_metadata(
                conn,
                checkpoint_id="ckpt",
                task_id=task_id,
                cost_microdollars=10,
                execution_metadata=None,
            )

    row = await _read_checkpoint(
        integration_pool, task_id=task_id, checkpoint_id="ckpt"
    )
    assert row["cost_microdollars"] == 10
    assert row["execution_metadata"] is None


@pytest.mark.asyncio
async def test_add_cost_and_preserve_metadata_is_additive(
    integration_pool: asyncpg.Pool,
) -> None:
    task_id = await _seed_task(integration_pool)
    await _insert_checkpoint(
        integration_pool, task_id=task_id, checkpoint_id="ckpt",
        cost_microdollars=100,
    )

    async with integration_pool.acquire() as conn:
        async with conn.transaction():
            await add_cost_and_preserve_metadata(
                conn,
                checkpoint_id="ckpt",
                task_id=task_id,
                delta_microdollars=50,
                execution_metadata={"irrelevant": True},
            )

    row = await _read_checkpoint(
        integration_pool, task_id=task_id, checkpoint_id="ckpt"
    )
    assert row["cost_microdollars"] == 150


@pytest.mark.asyncio
async def test_add_cost_and_preserve_metadata_keeps_existing_metadata(
    integration_pool: asyncpg.Pool,
) -> None:
    # This is load-bearing: sandbox-cleanup writes metadata onto the
    # terminal checkpoint first, then memory-write adds its own cost. If
    # COALESCE gets swapped for EXCLUDED-style overwrite, sandbox's metadata
    # disappears silently and the per-step timeline breaks.
    task_id = await _seed_task(integration_pool)
    await _insert_checkpoint(
        integration_pool, task_id=task_id, checkpoint_id="ckpt",
        cost_microdollars=10, execution_metadata={"prior": True},
    )

    async with integration_pool.acquire() as conn:
        async with conn.transaction():
            await add_cost_and_preserve_metadata(
                conn,
                checkpoint_id="ckpt",
                task_id=task_id,
                delta_microdollars=5,
                execution_metadata={"new": True},
            )

    row = await _read_checkpoint(
        integration_pool, task_id=task_id, checkpoint_id="ckpt"
    )
    assert json.loads(row["execution_metadata"]) == {"prior": True}


@pytest.mark.asyncio
async def test_add_cost_to_latest_terminal_checkpoint_targets_main_graph(
    integration_pool: asyncpg.Pool,
) -> None:
    task_id = await _seed_task(integration_pool)
    await _insert_checkpoint(
        integration_pool, task_id=task_id, checkpoint_id="ckpt-main-old",
        checkpoint_ns="", thread_ts="2026-04-16T00:00:00Z",
        cost_microdollars=100,
    )
    await asyncio.sleep(0.01)
    await _insert_checkpoint(
        integration_pool, task_id=task_id, checkpoint_id="ckpt-main-new",
        checkpoint_ns="", thread_ts="2026-04-16T00:00:01Z",
        cost_microdollars=200,
    )
    await _insert_checkpoint(
        integration_pool, task_id=task_id, checkpoint_id="ckpt-sub",
        checkpoint_ns="tools", thread_ts="2026-04-16T00:00:02Z",
        cost_microdollars=500,
    )

    async with integration_pool.acquire() as conn:
        async with conn.transaction():
            await add_cost_to_latest_terminal_checkpoint(
                conn, task_id=task_id, delta_microdollars=25,
            )

    new_main = await _read_checkpoint(
        integration_pool, task_id=task_id, checkpoint_id="ckpt-main-new",
    )
    old_main = await _read_checkpoint(
        integration_pool, task_id=task_id, checkpoint_id="ckpt-main-old",
    )
    sub = await _read_checkpoint(
        integration_pool, task_id=task_id, checkpoint_id="ckpt-sub",
    )
    assert new_main["cost_microdollars"] == 225
    assert old_main["cost_microdollars"] == 100
    # Non-empty ns checkpoint untouched even though it's newest overall.
    assert sub["cost_microdollars"] == 500
