"""Integration tests for PostgresDurableCheckpointer against a real PostgreSQL schema."""

from __future__ import annotations

import json
import os
import uuid

import asyncpg
import pytest

from checkpointer.postgres import LeaseRevokedException, PostgresDurableCheckpointer

DB_DSN = os.getenv(
    "E2E_DB_DSN",
    "postgresql://postgres:postgres@localhost:55432/persistent_agent_runtime",
)


@pytest.fixture
async def integration_pool():
    try:
        pool = await asyncpg.create_pool(DB_DSN, min_size=1, max_size=2)
    except Exception as exc:
        pytest.skip(f"PostgreSQL test database is not available: {exc}")

    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM checkpoint_writes")
        await conn.execute("DELETE FROM checkpoints")
        await conn.execute("DELETE FROM tasks")
        await conn.execute("DELETE FROM agents")

    try:
        yield pool
    finally:
        await pool.close()


async def _ensure_agent(
    pool: asyncpg.Pool,
    *,
    tenant_id: str = "default",
    agent_id: str = "agent",
) -> None:
    """Insert agent row if it doesn't exist (FK compliance)."""
    agent_config = json.dumps({
        "system_prompt": "You are a test assistant.",
        "model": "claude-sonnet-4-6",
        "temperature": 0.1,
        "allowed_tools": ["web_search"],
    })
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO agents (tenant_id, agent_id, display_name, agent_config, status)
            VALUES ($1, $2, 'Test Agent', $3::jsonb, 'active')
            ON CONFLICT (tenant_id, agent_id) DO NOTHING
            """,
            tenant_id, agent_id, agent_config,
        )


async def _insert_task(
    pool: asyncpg.Pool,
    *,
    task_id: str,
    lease_owner: str = "worker-integration",
    status: str = "running",
    tenant_id: str = "default",
    version: int = 1,
    dead_letter_reason: str | None = None,
) -> None:
    await _ensure_agent(pool, tenant_id=tenant_id)
    agent_config = {
        "system_prompt": "You are a test assistant.",
        "model": "claude-sonnet-4-6",
        "temperature": 0.1,
        "allowed_tools": ["web_search"],
    }
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO tasks (
                task_id,
                tenant_id,
                agent_id,
                agent_config_snapshot,
                status,
                input,
                lease_owner,
                lease_expiry,
                version,
                dead_letter_reason
            ) VALUES ($1::uuid, $2, 'agent', $3::jsonb, $4, 'input', $5, NOW() + INTERVAL '60 seconds', $6, $7)
            """,
            task_id,
            tenant_id,
            json.dumps(agent_config),
            status,
            lease_owner,
            version,
            dead_letter_reason,
        )


def _checkpoint(checkpoint_id: str, count: int) -> dict:
    return {
        "v": 1,
        "id": checkpoint_id,
        "ts": f"2026-03-07T10:00:0{count}.123456+00:00",
        "channel_values": {"count": count, "messages": [f"step-{count}"]},
        "channel_versions": {"count": str(count), "messages": str(count)},
        "versions_seen": {"agent": {"count": str(count - 1), "messages": str(count - 1)}},
        "updated_channels": ["count", "messages"],
    }


@pytest.mark.asyncio
async def test_valid_lease_write_and_get_tuple(integration_pool: asyncpg.Pool) -> None:
    task_id = str(uuid.uuid4())
    await _insert_task(integration_pool, task_id=task_id)
    saver = PostgresDurableCheckpointer(
        integration_pool,
        worker_id="worker-integration",
        tenant_id="default",
    )

    next_config = await saver.aput(
        {"configurable": {"thread_id": task_id, "checkpoint_ns": ""}},
        _checkpoint("checkpoint-001", 1),
        {"source": "loop", "step": 1},
        {"count": "1", "messages": "1"},
    )
    checkpoint_tuple = await saver.aget_tuple(
        {"configurable": {"thread_id": task_id, "checkpoint_ns": ""}}
    )

    assert next_config["configurable"]["checkpoint_id"] == "checkpoint-001"
    assert checkpoint_tuple is not None
    assert checkpoint_tuple.checkpoint["id"] == "checkpoint-001"
    assert checkpoint_tuple.metadata["step"] == 1


@pytest.mark.asyncio
async def test_revoked_lease_prevents_checkpoint_write(
    integration_pool: asyncpg.Pool,
) -> None:
    task_id = str(uuid.uuid4())
    await _insert_task(integration_pool, task_id=task_id)
    saver = PostgresDurableCheckpointer(
        integration_pool,
        worker_id="worker-integration",
        tenant_id="default",
    )

    await saver.aput(
        {"configurable": {"thread_id": task_id, "checkpoint_ns": ""}},
        _checkpoint("checkpoint-001", 1),
        {"source": "loop", "step": 1},
        {"count": "1"},
    )

    async with integration_pool.acquire() as conn:
        await conn.execute(
            "UPDATE tasks SET lease_owner = 'other-worker' WHERE task_id = $1::uuid",
            task_id,
        )

    with pytest.raises(LeaseRevokedException):
        await saver.aput(
            {
                "configurable": {
                    "thread_id": task_id,
                    "checkpoint_ns": "",
                    "checkpoint_id": "checkpoint-001",
                }
            },
            _checkpoint("checkpoint-002", 2),
            {"source": "loop", "step": 2},
            {"count": "2"},
        )

    async with integration_pool.acquire() as conn:
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM checkpoints WHERE task_id = $1::uuid",
            task_id,
        )
    assert count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "lease_owner", "dead_letter_reason"),
    [
        ("dead_letter", None, "cancelled_by_user"),
        ("completed", "worker-integration", None),
        ("queued", "worker-integration", None),
    ],
)
async def test_non_running_or_cancelled_task_prevents_checkpoint_write(
    integration_pool: asyncpg.Pool,
    status: str,
    lease_owner: str | None,
    dead_letter_reason: str | None,
) -> None:
    task_id = str(uuid.uuid4())
    await _insert_task(
        integration_pool,
        task_id=task_id,
        status=status,
        lease_owner=lease_owner,
        dead_letter_reason=dead_letter_reason,
    )
    saver = PostgresDurableCheckpointer(
        integration_pool,
        worker_id="worker-integration",
        tenant_id="default",
    )

    with pytest.raises(LeaseRevokedException):
        await saver.aput(
            {"configurable": {"thread_id": task_id, "checkpoint_ns": ""}},
            _checkpoint("checkpoint-001", 1),
            {"source": "loop", "step": 1},
            {"count": "1"},
        )

    async with integration_pool.acquire() as conn:
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM checkpoints WHERE task_id = $1::uuid",
            task_id,
        )
    assert count == 0


@pytest.mark.asyncio
async def test_tenant_mismatch_prevents_checkpoint_write(
    integration_pool: asyncpg.Pool,
) -> None:
    task_id = str(uuid.uuid4())
    await _insert_task(
        integration_pool,
        task_id=task_id,
        tenant_id="tenant-a",
    )
    saver = PostgresDurableCheckpointer(
        integration_pool,
        worker_id="worker-integration",
        tenant_id="default",
    )

    with pytest.raises(LeaseRevokedException):
        await saver.aput(
            {"configurable": {"thread_id": task_id, "checkpoint_ns": ""}},
            _checkpoint("checkpoint-001", 1),
            {"source": "loop", "step": 1},
            {"count": "1"},
        )

    async with integration_pool.acquire() as conn:
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM checkpoints WHERE task_id = $1::uuid",
            task_id,
        )
    assert count == 0


@pytest.mark.asyncio
async def test_version_is_not_used_in_lease_ownership_check(
    integration_pool: asyncpg.Pool,
) -> None:
    task_id = str(uuid.uuid4())
    await _insert_task(
        integration_pool,
        task_id=task_id,
        version=99,
    )
    saver = PostgresDurableCheckpointer(
        integration_pool,
        worker_id="worker-integration",
        tenant_id="default",
    )

    next_config = await saver.aput(
        {
            "configurable": {
                "thread_id": task_id,
                "checkpoint_ns": "",
                "version": 1,
            }
        },
        _checkpoint("checkpoint-001", 1),
        {"source": "loop", "step": 1},
        {"count": "1"},
    )

    assert next_config["configurable"]["checkpoint_id"] == "checkpoint-001"

    async with integration_pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT checkpoint_id, worker_id
            FROM checkpoints
            WHERE task_id = $1::uuid
            """,
            task_id,
        )
    assert row["checkpoint_id"] == "checkpoint-001"
    assert row["worker_id"] == "worker-integration"


@pytest.mark.asyncio
async def test_put_writes_persists_rows(integration_pool: asyncpg.Pool) -> None:
    task_id = str(uuid.uuid4())
    await _insert_task(integration_pool, task_id=task_id)
    saver = PostgresDurableCheckpointer(
        integration_pool,
        worker_id="worker-integration",
        tenant_id="default",
    )

    await saver.aput(
        {"configurable": {"thread_id": task_id, "checkpoint_ns": ""}},
        _checkpoint("checkpoint-001", 1),
        {"source": "loop", "step": 1},
        {"count": "1"},
    )
    await saver.aput_writes(
        {
            "configurable": {
                "thread_id": task_id,
                "checkpoint_ns": "",
                "checkpoint_id": "checkpoint-001",
            }
        },
        [("custom", {"value": 1}), ("other", {"value": 2})],
        task_id="writer-task",
        task_path="root/agent",
    )

    async with integration_pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT checkpoint_id, writer_task_id, task_path, idx, channel
            FROM checkpoint_writes
            WHERE task_id = $1::uuid
            ORDER BY idx
            """,
            task_id,
        )

    assert [row["checkpoint_id"] for row in rows] == ["checkpoint-001", "checkpoint-001"]
    assert [row["writer_task_id"] for row in rows] == ["writer-task", "writer-task"]
    assert [row["task_path"] for row in rows] == ["root/agent", "root/agent"]
    assert [row["channel"] for row in rows] == ["custom", "other"]


@pytest.mark.asyncio
async def test_revoked_lease_prevents_checkpoint_writes(
    integration_pool: asyncpg.Pool,
) -> None:
    task_id = str(uuid.uuid4())
    await _insert_task(integration_pool, task_id=task_id)
    saver = PostgresDurableCheckpointer(
        integration_pool,
        worker_id="worker-integration",
        tenant_id="default",
    )

    await saver.aput(
        {"configurable": {"thread_id": task_id, "checkpoint_ns": ""}},
        _checkpoint("checkpoint-001", 1),
        {"source": "loop", "step": 1},
        {"count": "1"},
    )

    async with integration_pool.acquire() as conn:
        await conn.execute(
            "UPDATE tasks SET lease_owner = 'other-worker' WHERE task_id = $1::uuid",
            task_id,
        )

    with pytest.raises(LeaseRevokedException):
        await saver.aput_writes(
            {
                "configurable": {
                    "thread_id": task_id,
                    "checkpoint_ns": "",
                    "checkpoint_id": "checkpoint-001",
                }
            },
            [("custom", {"value": 1})],
            task_id="writer-task",
            task_path="root/agent",
        )

    async with integration_pool.acquire() as conn:
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM checkpoint_writes WHERE task_id = $1::uuid",
            task_id,
        )

    assert count == 0
