"""Integration tests for Phase 2 Track 5 Task 8 — follow-up/redrive seeding.

Covers :func:`core.memory_repository.read_memory_observations_by_task_id`,
the single source of truth for "what observations did the prior execution
produce?". The worker reads this at the top of ``execute_task`` and seeds
``MemoryEnabledState.observations`` via the graph's initial state argument
so the ``operator.add`` reducer preserves prior observations across
follow-up / redrive.

Tests:

* **First run** — no prior memory row for the task → helper returns
  ``None``. Caller interprets as "start with empty observations".
* **Second run (after successful commit)** — helper returns the observations
  committed by the prior run, in order.
* **Redrive after dead-letter** — helper returns the observations from the
  ``template:dead_letter`` row written by Task 8's dead-letter hook.
* **Cross-scope scope binding** — same ``task_id`` in a different
  ``(tenant_id, agent_id)`` scope is invisible to the helper.

Runs against the isolated test DB on port 55433.
"""

from __future__ import annotations

import json
import os
import uuid

import asyncpg
import pytest

from core.memory_repository import (
    read_memory_observations_by_task_id,
    upsert_memory_entry,
)


DB_DSN = os.getenv(
    "E2E_DB_DSN",
    "postgresql://postgres:postgres@localhost:55433/persistent_agent_runtime_e2e",
)

TENANT_ID = "default"
AGENT_ID = "memory-follow-up-test-agent"
OTHER_AGENT = "memory-follow-up-other-agent"


async def _scrub(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM agent_memory_entries WHERE tenant_id = $1", TENANT_ID
        )
        await conn.execute(
            "DELETE FROM agents WHERE tenant_id = $1 "
            "AND agent_id = ANY($2::text[])",
            TENANT_ID, [AGENT_ID, OTHER_AGENT],
        )


@pytest.fixture
async def integration_pool():
    try:
        pool = await asyncpg.create_pool(DB_DSN, min_size=1, max_size=2)
    except Exception as exc:
        pytest.skip(f"PostgreSQL test database is not available: {exc}")
    await _scrub(pool)
    async with pool.acquire() as conn:
        for agent in (AGENT_ID, OTHER_AGENT):
            await conn.execute(
                """
                INSERT INTO agents (tenant_id, agent_id, display_name,
                                    agent_config, status)
                VALUES ($1, $2, 'Follow-up Seed Test', '{}'::jsonb, 'active')
                ON CONFLICT (tenant_id, agent_id) DO NOTHING
                """,
                TENANT_ID, agent,
            )
    try:
        yield pool
    finally:
        await _scrub(pool)
        await pool.close()


async def _commit_memory_row(
    pool: asyncpg.Pool,
    *,
    agent_id: str,
    task_id: str,
    observations: list[str],
    outcome: str = "succeeded",
    summarizer_model_id: str = "claude-haiku-4-5",
) -> None:
    async with pool.acquire() as conn:
        async with conn.transaction():
            await upsert_memory_entry(
                conn,
                {
                    "tenant_id": TENANT_ID,
                    "agent_id": agent_id,
                    "task_id": task_id,
                    "title": "t",
                    "summary": "s",
                    "observations": list(observations),
                    "outcome": outcome,
                    "tags": [],
                    "content_vec": None,
                    "summarizer_model_id": summarizer_model_id,
                },
            )


# ---------------------------------------------------------------------------


class TestFirstRunReturnsNone:
    @pytest.mark.asyncio
    async def test_no_memory_row_returns_none(
        self, integration_pool: asyncpg.Pool,
    ) -> None:
        task_id = str(uuid.uuid4())
        async with integration_pool.acquire() as conn:
            obs = await read_memory_observations_by_task_id(
                conn, TENANT_ID, AGENT_ID, task_id,
            )
        assert obs is None


class TestSecondRunAfterSuccessfulWrite:
    @pytest.mark.asyncio
    async def test_returns_prior_observations_verbatim(
        self, integration_pool: asyncpg.Pool,
    ) -> None:
        task_id = str(uuid.uuid4())
        await _commit_memory_row(
            integration_pool,
            agent_id=AGENT_ID,
            task_id=task_id,
            observations=["obs-1", "obs-2", "obs-3"],
        )

        async with integration_pool.acquire() as conn:
            obs = await read_memory_observations_by_task_id(
                conn, TENANT_ID, AGENT_ID, task_id,
            )
        assert obs == ["obs-1", "obs-2", "obs-3"]

    @pytest.mark.asyncio
    async def test_empty_observations_row_returns_empty_list_not_none(
        self, integration_pool: asyncpg.Pool,
    ) -> None:
        task_id = str(uuid.uuid4())
        await _commit_memory_row(
            integration_pool,
            agent_id=AGENT_ID,
            task_id=task_id,
            observations=[],
        )
        async with integration_pool.acquire() as conn:
            obs = await read_memory_observations_by_task_id(
                conn, TENANT_ID, AGENT_ID, task_id,
            )
        # Distinguishes "no row" (None) from "row exists, empty" ([]).
        assert obs == []


class TestRedriveAfterDeadLetter:
    @pytest.mark.asyncio
    async def test_returns_observations_from_template_dead_letter_row(
        self, integration_pool: asyncpg.Pool,
    ) -> None:
        task_id = str(uuid.uuid4())
        # Simulate Task 8 dead-letter hook having written a template row.
        await _commit_memory_row(
            integration_pool,
            agent_id=AGENT_ID,
            task_id=task_id,
            observations=["dl-obs-1", "dl-obs-2"],
            outcome="failed",
            summarizer_model_id="template:dead_letter",
        )
        async with integration_pool.acquire() as conn:
            obs = await read_memory_observations_by_task_id(
                conn, TENANT_ID, AGENT_ID, task_id,
            )
        assert obs == ["dl-obs-1", "dl-obs-2"]


class TestScopeBinding:
    @pytest.mark.asyncio
    async def test_same_task_id_different_agent_is_invisible(
        self, integration_pool: asyncpg.Pool,
    ) -> None:
        task_id = str(uuid.uuid4())
        # Write a row under OTHER_AGENT.
        await _commit_memory_row(
            integration_pool,
            agent_id=OTHER_AGENT,
            task_id=task_id,
            observations=["leaked?"],
        )
        # Lookup under AGENT_ID with the same task_id must not see it.
        async with integration_pool.acquire() as conn:
            obs = await read_memory_observations_by_task_id(
                conn, TENANT_ID, AGENT_ID, task_id,
            )
        assert obs is None

    @pytest.mark.asyncio
    async def test_cross_tenant_read_returns_none(
        self, integration_pool: asyncpg.Pool,
    ) -> None:
        task_id = str(uuid.uuid4())
        await _commit_memory_row(
            integration_pool,
            agent_id=AGENT_ID,
            task_id=task_id,
            observations=["x"],
        )
        async with integration_pool.acquire() as conn:
            obs = await read_memory_observations_by_task_id(
                conn, "other-tenant", AGENT_ID, task_id,
            )
        assert obs is None
