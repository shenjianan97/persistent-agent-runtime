"""Integration tests for the 0012 migration's defense-in-depth CHECK constraints
on ``agent_memory_entries`` (Phase 2 Track 5).

Tool-layer caps exist (2 KB per ``memory_note``, ~4 KB typical summary) but an
application bug or a crafted write path could bypass those caps and store
arbitrarily large blobs. The 0012 migration adds DB-level outer-envelope bounds:

- ``summary`` length <= 8192 (2x design-expected, gives format-evolution headroom)
- ``observations`` cardinality <= 1000
- per-element ``observations`` length <= 4096 (via IMMUTABLE helper function)

These run against the isolated test DB on port 55433 (``make worker-test``).
"""

from __future__ import annotations

import os
import uuid

import asyncpg
import pytest


DB_DSN = os.getenv(
    "E2E_DB_DSN",
    "postgresql://postgres:postgres@localhost:55433/persistent_agent_runtime_e2e",
)

TENANT_ID = "default"
AGENT_ID = "memory-check-constraints-test-agent"


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
            "DELETE FROM agents WHERE tenant_id = $1 AND agent_id = $2",
            TENANT_ID, AGENT_ID,
        )
        await conn.execute(
            """
            INSERT INTO agents (tenant_id, agent_id, display_name, agent_config, status)
            VALUES ($1, $2, 'Memory CHECK Constraints Test Agent', '{}'::jsonb, 'active')
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


async def _insert(
    conn: asyncpg.Connection,
    *,
    summary: str,
    observations: list[str],
) -> uuid.UUID:
    """Insert one row; returns memory_id. Raises on constraint violation."""
    task_id = str(uuid.uuid4())
    memory_id = await conn.fetchval(
        """
        INSERT INTO agent_memory_entries (
            tenant_id, agent_id, task_id,
            title, summary, observations, outcome, tags
        )
        VALUES ($1, $2, $3::uuid, $4, $5, $6, 'succeeded', '{}')
        RETURNING memory_id
        """,
        TENANT_ID, AGENT_ID, task_id, "Title", summary, observations,
    )
    return memory_id


class TestSummaryLengthConstraint:
    @pytest.mark.asyncio
    async def test_summary_at_upper_bound_is_accepted(
        self, integration_pool: asyncpg.Pool
    ) -> None:
        """A summary exactly 8192 bytes long is within the cap."""
        async with integration_pool.acquire() as conn:
            memory_id = await _insert(conn, summary="a" * 8192, observations=[])
        assert isinstance(memory_id, uuid.UUID)

    @pytest.mark.asyncio
    async def test_summary_just_over_cap_is_rejected(
        self, integration_pool: asyncpg.Pool
    ) -> None:
        """8193-byte summary trips the CHECK."""
        async with integration_pool.acquire() as conn:
            with pytest.raises(asyncpg.CheckViolationError):
                await _insert(conn, summary="a" * 8193, observations=[])

    @pytest.mark.asyncio
    async def test_ten_kb_summary_is_rejected(
        self, integration_pool: asyncpg.Pool
    ) -> None:
        """A 10 KB blob must be rejected — matches the task-spec test case."""
        async with integration_pool.acquire() as conn:
            with pytest.raises(asyncpg.CheckViolationError):
                await _insert(conn, summary="x" * 10_240, observations=[])


class TestObservationsCardinalityConstraint:
    @pytest.mark.asyncio
    async def test_exactly_1000_observations_is_accepted(
        self, integration_pool: asyncpg.Pool
    ) -> None:
        async with integration_pool.acquire() as conn:
            memory_id = await _insert(
                conn,
                summary="ok",
                observations=[f"obs-{i}" for i in range(1000)],
            )
        assert isinstance(memory_id, uuid.UUID)

    @pytest.mark.asyncio
    async def test_1001_element_observations_is_rejected(
        self, integration_pool: asyncpg.Pool
    ) -> None:
        """Matches the task-spec test case."""
        async with integration_pool.acquire() as conn:
            with pytest.raises(asyncpg.CheckViolationError):
                await _insert(
                    conn,
                    summary="ok",
                    observations=[f"obs-{i}" for i in range(1001)],
                )


class TestObservationElementLengthConstraint:
    @pytest.mark.asyncio
    async def test_observation_element_at_bound_is_accepted(
        self, integration_pool: asyncpg.Pool
    ) -> None:
        async with integration_pool.acquire() as conn:
            memory_id = await _insert(
                conn, summary="ok", observations=["a" * 4096]
            )
        assert isinstance(memory_id, uuid.UUID)

    @pytest.mark.asyncio
    async def test_oversized_observation_element_is_rejected(
        self, integration_pool: asyncpg.Pool
    ) -> None:
        async with integration_pool.acquire() as conn:
            with pytest.raises(asyncpg.CheckViolationError):
                await _insert(
                    conn, summary="ok", observations=["a" * 4097]
                )

    @pytest.mark.asyncio
    async def test_one_oversized_element_in_mixed_array_is_rejected(
        self, integration_pool: asyncpg.Pool
    ) -> None:
        """A single oversized element among shorter ones still fails."""
        async with integration_pool.acquire() as conn:
            with pytest.raises(asyncpg.CheckViolationError):
                await _insert(
                    conn,
                    summary="ok",
                    observations=["short", "also short", "x" * 4097],
                )

    @pytest.mark.asyncio
    async def test_multibyte_summary_enforces_byte_cap_not_char_cap(
        self, integration_pool: asyncpg.Pool
    ) -> None:
        """CJK/emoji text encoded as UTF-8 is 3-4 bytes per character.

        A string of 3000 CJK characters is ~9000 bytes — well over the
        8192-byte cap even though it's under 8192 *characters*. The
        constraint uses ``octet_length`` so storage size, not glyph count,
        governs the bound.
        """
        cjk_over_cap = "日" * 3000  # 3 bytes each in UTF-8 → 9000 bytes
        async with integration_pool.acquire() as conn:
            with pytest.raises(asyncpg.CheckViolationError):
                await _insert(conn, summary=cjk_over_cap, observations=[])

    @pytest.mark.asyncio
    async def test_multibyte_observation_element_enforces_byte_cap(
        self, integration_pool: asyncpg.Pool
    ) -> None:
        """Same byte-vs-character distinction for the per-observation cap."""
        cjk_over_cap = "日" * 1500  # 3 bytes each → 4500 bytes, > 4096
        async with integration_pool.acquire() as conn:
            with pytest.raises(asyncpg.CheckViolationError):
                await _insert(
                    conn, summary="ok", observations=[cjk_over_cap]
                )


class TestValidRowStillAccepted:
    @pytest.mark.asyncio
    async def test_typical_row_accepted(
        self, integration_pool: asyncpg.Pool
    ) -> None:
        """A realistic row within all bounds succeeds."""
        async with integration_pool.acquire() as conn:
            memory_id = await _insert(
                conn,
                summary="This is a typical retrospective summary.",
                observations=[
                    "agent noticed X",
                    "agent chose Y because Z",
                    "final output referenced API version 4.7",
                ],
            )
        assert isinstance(memory_id, uuid.UUID)
