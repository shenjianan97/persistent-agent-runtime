"""Integration tests for ``core/memory_repository.py`` (Phase 2 Track 5 Task 6).

Exercises the asyncpg helpers that own the ``agent_memory_entries`` write path:

- ``upsert_memory_entry`` — INSERT vs UPDATE branch differentiation via
  ``xmax = 0``; UPSERT preserves ``created_at`` but advances ``updated_at`` and
  ``version`` on the UPDATE branch.
- ``count_entries_for_agent`` — scoped by ``(tenant_id, agent_id)``.
- ``trim_oldest`` — removes oldest rows first by ``(created_at ASC, memory_id
  ASC)``, and excludes a passed-in ``keep_memory_id`` from the eviction set.
- ``max_entries_for_agent`` — clamps ``agent_config.memory.max_entries`` to
  ``[100, 100_000]`` with a platform default of ``10_000``.

These run against the isolated test DB on port 55433 (``make worker-test``).
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from datetime import datetime, timedelta, timezone

import asyncpg
import pytest

from core.memory_repository import (
    count_entries_for_agent,
    max_entries_for_agent,
    read_pending_memory_from_state_values,
    trim_oldest,
    upsert_memory_entry,
)


DB_DSN = os.getenv(
    "E2E_DB_DSN",
    "postgresql://postgres:postgres@localhost:55433/persistent_agent_runtime_e2e",
)

TENANT_ID = "default"
AGENT_ID = "memory-repo-test-agent"


@pytest.fixture
async def integration_pool():
    try:
        pool = await asyncpg.create_pool(DB_DSN, min_size=1, max_size=2)
    except Exception as exc:
        pytest.skip(f"PostgreSQL test database is not available: {exc}")

    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM agent_memory_entries WHERE tenant_id = $1", TENANT_ID
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
            VALUES ($1, $2, 'Memory Repo Test Agent', '{}'::jsonb, 'active')
            """,
            TENANT_ID, AGENT_ID,
        )

    try:
        yield pool
    finally:
        async with pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM agent_memory_entries WHERE tenant_id = $1",
                TENANT_ID,
            )
            await conn.execute(
                "DELETE FROM agents WHERE tenant_id = $1 AND agent_id = $2",
                TENANT_ID, AGENT_ID,
            )
        await pool.close()


def _entry(
    *,
    task_id: str,
    title: str = "Title",
    summary: str = "Summary",
    observations: list[str] | None = None,
    outcome: str = "succeeded",
    tags: list[str] | None = None,
    content_vec: list[float] | None = None,
    summarizer_model_id: str = "claude-haiku-4-5",
) -> dict:
    return {
        "tenant_id": TENANT_ID,
        "agent_id": AGENT_ID,
        "task_id": task_id,
        "title": title,
        "summary": summary,
        "observations": observations or [],
        "outcome": outcome,
        "tags": tags or [],
        "content_vec": content_vec,
        "summarizer_model_id": summarizer_model_id,
    }


class TestUpsertMemoryEntry:
    @pytest.mark.asyncio
    async def test_insert_branch_reports_inserted_true(
        self, integration_pool: asyncpg.Pool
    ) -> None:
        task_id = str(uuid.uuid4())
        async with integration_pool.acquire() as conn:
            async with conn.transaction():
                result = await upsert_memory_entry(conn, _entry(task_id=task_id))

        assert result["inserted"] is True
        assert isinstance(result["memory_id"], uuid.UUID)

    @pytest.mark.asyncio
    async def test_second_upsert_same_task_id_takes_update_branch(
        self, integration_pool: asyncpg.Pool
    ) -> None:
        task_id = str(uuid.uuid4())
        async with integration_pool.acquire() as conn:
            async with conn.transaction():
                first = await upsert_memory_entry(conn, _entry(task_id=task_id))

            # Sleep a millisecond so updated_at can advance beyond created_at
            await asyncio.sleep(0.01)

            async with conn.transaction():
                second = await upsert_memory_entry(
                    conn,
                    _entry(
                        task_id=task_id,
                        title="Updated",
                        summary="Newer summary",
                    ),
                )

        assert first["inserted"] is True
        assert second["inserted"] is False
        # Same memory_id — UPSERT matched on task_id
        assert first["memory_id"] == second["memory_id"]

        async with integration_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT title, summary, version, created_at, updated_at "
                "FROM agent_memory_entries WHERE task_id = $1::uuid",
                task_id,
            )
        assert row["title"] == "Updated"
        assert row["summary"] == "Newer summary"
        assert row["version"] == 2
        assert row["updated_at"] > row["created_at"]

    @pytest.mark.asyncio
    async def test_update_branch_preserves_created_at(
        self, integration_pool: asyncpg.Pool
    ) -> None:
        task_id = str(uuid.uuid4())
        async with integration_pool.acquire() as conn:
            async with conn.transaction():
                await upsert_memory_entry(conn, _entry(task_id=task_id))

            original_created_at = await conn.fetchval(
                "SELECT created_at FROM agent_memory_entries WHERE task_id = $1::uuid",
                task_id,
            )

            await asyncio.sleep(0.01)
            async with conn.transaction():
                await upsert_memory_entry(
                    conn, _entry(task_id=task_id, title="Second")
                )

            after_update_created_at = await conn.fetchval(
                "SELECT created_at FROM agent_memory_entries WHERE task_id = $1::uuid",
                task_id,
            )

        assert original_created_at == after_update_created_at

    @pytest.mark.asyncio
    async def test_accepts_null_content_vec(
        self, integration_pool: asyncpg.Pool
    ) -> None:
        task_id = str(uuid.uuid4())
        async with integration_pool.acquire() as conn:
            async with conn.transaction():
                result = await upsert_memory_entry(
                    conn, _entry(task_id=task_id, content_vec=None)
                )

            vec = await conn.fetchval(
                "SELECT content_vec FROM agent_memory_entries "
                "WHERE memory_id = $1",
                result["memory_id"],
            )

        assert vec is None


class TestTrimOldest:
    @pytest.mark.asyncio
    async def test_trim_does_nothing_when_below_cap(
        self, integration_pool: asyncpg.Pool
    ) -> None:
        task_id = str(uuid.uuid4())
        async with integration_pool.acquire() as conn:
            async with conn.transaction():
                result = await upsert_memory_entry(conn, _entry(task_id=task_id))

                evicted = await trim_oldest(
                    conn,
                    tenant_id=TENANT_ID,
                    agent_id=AGENT_ID,
                    max_entries=10,
                    keep_memory_id=result["memory_id"],
                )

        assert evicted == 0

    @pytest.mark.asyncio
    async def test_trim_evicts_oldest_first_and_keeps_fresh_row(
        self, integration_pool: asyncpg.Pool
    ) -> None:
        # Seed 5 rows with hand-rolled, increasing created_at timestamps.
        base = datetime.now(timezone.utc) - timedelta(days=5)
        task_ids = [str(uuid.uuid4()) for _ in range(5)]
        memory_ids: list[uuid.UUID] = []

        async with integration_pool.acquire() as conn:
            for i, task_id in enumerate(task_ids):
                memory_id = uuid.uuid4()
                memory_ids.append(memory_id)
                await conn.execute(
                    """
                    INSERT INTO agent_memory_entries
                        (memory_id, tenant_id, agent_id, task_id,
                         title, summary, observations, outcome, tags,
                         content_vec, summarizer_model_id, created_at, updated_at)
                    VALUES ($1, $2, $3, $4::uuid,
                            $5, 'summary', '{}'::text[], 'succeeded', '{}'::text[],
                            NULL, 'template:fallback', $6, $6)
                    """,
                    memory_id,
                    TENANT_ID, AGENT_ID, task_id,
                    f"Row {i}",
                    base + timedelta(hours=i),
                )

            fresh_id = memory_ids[-1]  # newest row — must not be evicted.

            async with conn.transaction():
                evicted = await trim_oldest(
                    conn,
                    tenant_id=TENANT_ID,
                    agent_id=AGENT_ID,
                    max_entries=3,
                    keep_memory_id=fresh_id,
                )

            remaining_ids = {
                row["memory_id"]
                for row in await conn.fetch(
                    "SELECT memory_id FROM agent_memory_entries "
                    "WHERE tenant_id = $1 AND agent_id = $2",
                    TENANT_ID, AGENT_ID,
                )
            }

        # 5 rows - max_entries=3 → 2 should be evicted (the oldest two).
        assert evicted == 2
        assert fresh_id in remaining_ids
        assert memory_ids[0] not in remaining_ids
        assert memory_ids[1] not in remaining_ids
        # The middle three (i=2,3,4) should remain.
        assert memory_ids[2] in remaining_ids
        assert memory_ids[3] in remaining_ids
        assert memory_ids[4] in remaining_ids

    @pytest.mark.asyncio
    async def test_trim_never_evicts_keep_memory_id_even_if_oldest(
        self, integration_pool: asyncpg.Pool
    ) -> None:
        """The 'keep' row is the one we just inserted. FIFO would otherwise
        evict a row with the oldest created_at — but an insert that races with
        a pre-existing row's timestamp (or clock skew) could make the fresh
        row the oldest. The repo must honour the keep_memory_id exclusion.
        """
        base = datetime.now(timezone.utc)
        task_ids = [str(uuid.uuid4()) for _ in range(4)]
        memory_ids: list[uuid.UUID] = []

        async with integration_pool.acquire() as conn:
            # Row 0 is the "keep" row, inserted with the OLDEST timestamp.
            for i, task_id in enumerate(task_ids):
                memory_id = uuid.uuid4()
                memory_ids.append(memory_id)
                await conn.execute(
                    """
                    INSERT INTO agent_memory_entries
                        (memory_id, tenant_id, agent_id, task_id,
                         title, summary, observations, outcome, tags,
                         content_vec, summarizer_model_id, created_at, updated_at)
                    VALUES ($1, $2, $3, $4::uuid,
                            $5, 'summary', '{}'::text[], 'succeeded', '{}'::text[],
                            NULL, 'template:fallback', $6, $6)
                    """,
                    memory_id,
                    TENANT_ID, AGENT_ID, task_id,
                    f"Row {i}",
                    base + timedelta(seconds=i),
                )

            keep_id = memory_ids[0]

            async with conn.transaction():
                evicted = await trim_oldest(
                    conn,
                    tenant_id=TENANT_ID,
                    agent_id=AGENT_ID,
                    max_entries=2,
                    keep_memory_id=keep_id,
                )

            remaining_ids = {
                row["memory_id"]
                for row in await conn.fetch(
                    "SELECT memory_id FROM agent_memory_entries "
                    "WHERE tenant_id = $1 AND agent_id = $2",
                    TENANT_ID, AGENT_ID,
                )
            }

        # 4 rows (including keep) - cap 2 → 2 evicted from the NON-keep set.
        assert evicted == 2
        assert keep_id in remaining_ids
        # Rows 1 and 2 are the next-oldest non-keep rows → evicted.
        assert memory_ids[1] not in remaining_ids
        assert memory_ids[2] not in remaining_ids
        # Row 3 is the newest non-keep row → preserved.
        assert memory_ids[3] in remaining_ids


class TestCountEntriesForAgent:
    @pytest.mark.asyncio
    async def test_scoped_by_tenant_and_agent(
        self, integration_pool: asyncpg.Pool
    ) -> None:
        task_a = str(uuid.uuid4())
        task_b = str(uuid.uuid4())
        async with integration_pool.acquire() as conn:
            async with conn.transaction():
                await upsert_memory_entry(conn, _entry(task_id=task_a))
                await upsert_memory_entry(conn, _entry(task_id=task_b))

            count = await count_entries_for_agent(conn, TENANT_ID, AGENT_ID)
            zero = await count_entries_for_agent(
                conn, TENANT_ID, "some-other-agent-that-does-not-exist"
            )

        assert count == 2
        assert zero == 0


class TestMaxEntriesForAgent:
    def test_default_when_memory_section_absent(self) -> None:
        assert max_entries_for_agent({}) == 10_000

    def test_default_when_memory_section_missing_max(self) -> None:
        assert max_entries_for_agent({"memory": {"enabled": True}}) == 10_000

    def test_explicit_value_passes_through(self) -> None:
        assert (
            max_entries_for_agent({"memory": {"max_entries": 250}}) == 250
        )

    def test_value_below_floor_is_clamped_to_100(self) -> None:
        assert max_entries_for_agent({"memory": {"max_entries": 5}}) == 100

    def test_value_above_ceiling_is_clamped_to_100000(self) -> None:
        assert (
            max_entries_for_agent({"memory": {"max_entries": 5_000_000}})
            == 100_000
        )

    def test_non_integer_value_falls_back_to_default(self) -> None:
        assert (
            max_entries_for_agent({"memory": {"max_entries": "not-a-number"}})
            == 10_000
        )


class TestReadPendingMemoryFromStateValues:
    def test_returns_pending_memory_when_present(self) -> None:
        values = {"messages": [], "pending_memory": {"title": "T"}}
        assert read_pending_memory_from_state_values(values) == {"title": "T"}

    def test_returns_none_when_absent(self) -> None:
        assert read_pending_memory_from_state_values({"messages": []}) is None

    def test_returns_none_when_explicitly_none(self) -> None:
        assert (
            read_pending_memory_from_state_values({"pending_memory": None})
            is None
        )

    def test_returns_none_when_values_none(self) -> None:
        assert read_pending_memory_from_state_values(None) is None
