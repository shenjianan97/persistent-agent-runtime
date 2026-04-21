"""Phase 2 Track 7 Follow-up Task 8 (D) — backfill CLI integration tests.

Runs against the isolated test DB on port 55433. Exercises the three
invariants from §Backfill in the design doc:

* Only non-terminal tasks are touched (completed / dead_letter skipped).
* Idempotence — a repeat run produces exactly the same row count.
* Unknown convlog kinds are skipped without error.
"""

from __future__ import annotations

import os
import uuid

import asyncpg
import pytest

from scripts.backfill_convlog_to_task_events import (
    _backfill_key,
    backfill,
)


DB_DSN = os.getenv(
    "E2E_DB_DSN",
    "postgresql://postgres:postgres@localhost:55433/persistent_agent_runtime_e2e",
)

TENANT_ID = "default"
AGENT_ID = "backfill-test-agent"


@pytest.fixture
async def pool():
    try:
        pool = await asyncpg.create_pool(DB_DSN, min_size=1, max_size=2)
    except Exception as exc:
        pytest.skip(f"PostgreSQL test database is not available: {exc}")

    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM task_conversation_log WHERE tenant_id = $1", TENANT_ID
        )
        await conn.execute(
            "DELETE FROM task_events WHERE tenant_id = $1 AND agent_id = $2",
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
            VALUES ($1, $2, 'Backfill Test', '{}'::jsonb, 'active')
            """,
            TENANT_ID, AGENT_ID,
        )

    try:
        yield pool
    finally:
        async with pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM task_conversation_log WHERE tenant_id = $1", TENANT_ID
            )
            await conn.execute(
                "DELETE FROM task_events WHERE tenant_id = $1 AND agent_id = $2",
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


async def _seed_task(pool: asyncpg.Pool, *, status: str) -> str:
    task_id = str(uuid.uuid4())
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO tasks (
                task_id, tenant_id, agent_id, agent_config_snapshot,
                status, input, version
            ) VALUES ($1::uuid, $2, $3, '{}'::jsonb, $4, 'input', 1)
            """,
            task_id, TENANT_ID, AGENT_ID, status,
        )
    return task_id


async def _seed_convlog(
    pool: asyncpg.Pool,
    *,
    task_id: str,
    kind: str,
    content: dict,
    metadata: dict | None = None,
) -> str:
    entry_id = str(uuid.uuid4())
    idem = f"{entry_id}:{kind}"
    size = len(str(content))
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO task_conversation_log (
                entry_id, tenant_id, task_id, checkpoint_id,
                idempotency_key, kind, role, content, content_size, metadata
            ) VALUES ($1::uuid, $2, $3::uuid, NULL, $4, $5, 'system',
                      $6::jsonb, $7, $8::jsonb)
            """,
            entry_id, TENANT_ID, task_id, idem, kind,
            __import__("json").dumps(content), size,
            __import__("json").dumps(metadata or {}),
        )
    return entry_id


def test_backfill_key_deterministic() -> None:
    k1 = _backfill_key("task-a", "entry-1", "memory_flush")
    k2 = _backfill_key("task-a", "entry-1", "memory_flush")
    assert k1 == k2
    # Different inputs yield different keys.
    assert k1 != _backfill_key("task-b", "entry-1", "memory_flush")
    assert k1 != _backfill_key("task-a", "entry-2", "memory_flush")
    assert k1 != _backfill_key("task-a", "entry-1", "offload_emitted")


@pytest.mark.asyncio
async def test_backfill_copies_three_marker_kinds_to_task_events(
    pool: asyncpg.Pool,
) -> None:
    task_id = await _seed_task(pool, status="running")
    await _seed_convlog(pool, task_id=task_id, kind="memory_flush",
                        content={}, metadata={"fired_at_step": 7})
    await _seed_convlog(pool, task_id=task_id, kind="offload_emitted",
                        content={"count": 2, "total_bytes": 2048,
                                 "step_index": 10})
    await _seed_convlog(pool, task_id=task_id, kind="system_note",
                        content={"text": "deploy 2026-04-20"})

    stats = await backfill(dsn=DB_DSN, dry_run=False, tenant=TENANT_ID, limit=100)
    assert stats["tasks"] == 1
    assert stats["rows_scanned"] == 3
    assert stats["rows_inserted"] == 3
    assert stats["rows_skipped_dedup"] == 0

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT event_type, details::jsonb AS details FROM task_events "
            "WHERE task_id = $1::uuid ORDER BY event_type",
            task_id,
        )
    import json as _json
    event_types = {r["event_type"] for r in rows}
    assert event_types == {"memory_flush", "offload_emitted", "system_note"}
    for r in rows:
        details = _json.loads(r["details"]) if isinstance(r["details"], str) else r["details"]
        assert details["backfilled_from_convlog"] is True
        assert len(details["backfill_key"]) == 64  # sha256 hex


@pytest.mark.asyncio
async def test_backfill_skips_terminal_status_tasks(pool: asyncpg.Pool) -> None:
    """Completed / dead_letter tasks are not touched."""
    completed_id = await _seed_task(pool, status="completed")
    await _seed_convlog(pool, task_id=completed_id, kind="memory_flush",
                        content={}, metadata={"fired_at_step": 1})

    dead_id = await _seed_task(pool, status="dead_letter")
    await _seed_convlog(pool, task_id=dead_id, kind="offload_emitted",
                        content={"count": 1, "total_bytes": 100, "step_index": 2})

    running_id = await _seed_task(pool, status="running")
    await _seed_convlog(pool, task_id=running_id, kind="memory_flush",
                        content={}, metadata={"fired_at_step": 3})

    stats = await backfill(dsn=DB_DSN, dry_run=False, tenant=TENANT_ID, limit=100)
    assert stats["tasks"] == 1  # only running
    assert stats["rows_inserted"] == 1

    async with pool.acquire() as conn:
        completed_events = await conn.fetch(
            "SELECT 1 FROM task_events WHERE task_id = $1::uuid", completed_id
        )
        dead_events = await conn.fetch(
            "SELECT 1 FROM task_events WHERE task_id = $1::uuid", dead_id
        )
    assert completed_events == []
    assert dead_events == []


@pytest.mark.asyncio
async def test_backfill_is_idempotent_across_runs(pool: asyncpg.Pool) -> None:
    task_id = await _seed_task(pool, status="paused")
    await _seed_convlog(pool, task_id=task_id, kind="memory_flush",
                        content={}, metadata={"fired_at_step": 1})

    first = await backfill(dsn=DB_DSN, dry_run=False, tenant=TENANT_ID, limit=100)
    second = await backfill(dsn=DB_DSN, dry_run=False, tenant=TENANT_ID, limit=100)

    assert first["rows_inserted"] == 1
    assert second["rows_inserted"] == 0
    assert second["rows_skipped_dedup"] == 1

    async with pool.acquire() as conn:
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM task_events WHERE task_id = $1::uuid", task_id
        )
    assert count == 1


@pytest.mark.asyncio
async def test_backfill_dry_run_does_not_write(pool: asyncpg.Pool) -> None:
    task_id = await _seed_task(pool, status="queued")
    await _seed_convlog(pool, task_id=task_id, kind="memory_flush",
                        content={}, metadata={"fired_at_step": 1})

    stats = await backfill(dsn=DB_DSN, dry_run=True, tenant=TENANT_ID, limit=100)
    assert stats["rows_inserted"] == 1  # counted as planned

    async with pool.acquire() as conn:
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM task_events WHERE task_id = $1::uuid", task_id
        )
    assert count == 0  # nothing actually written


@pytest.mark.asyncio
async def test_backfill_ignores_nonbackfill_kinds(pool: asyncpg.Pool) -> None:
    """user_turn / compaction_boundary / hitl_pause are out of backfill scope."""
    task_id = await _seed_task(pool, status="running")
    await _seed_convlog(pool, task_id=task_id, kind="user_turn",
                        content={"text": "ignore me"})
    await _seed_convlog(pool, task_id=task_id, kind="compaction_boundary",
                        content={"summary_text": "ignore me too"})
    await _seed_convlog(pool, task_id=task_id, kind="hitl_pause",
                        content={"reason": "ignore"})
    # One valid kind mixed in.
    await _seed_convlog(pool, task_id=task_id, kind="system_note",
                        content={"text": "pick me"})

    stats = await backfill(dsn=DB_DSN, dry_run=False, tenant=TENANT_ID, limit=100)
    # SELECT filters kinds server-side → only the one valid row is scanned.
    assert stats["rows_scanned"] == 1
    assert stats["rows_inserted"] == 1

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT event_type FROM task_events WHERE task_id = $1::uuid", task_id
        )
    assert [r["event_type"] for r in rows] == ["system_note"]
