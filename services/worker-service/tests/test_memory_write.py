"""Integration tests for the Phase 2 Track 5 Task 6 commit path.

The commit path lives on :class:`executor.graph.GraphExecutor` as
:py:meth:`_commit_memory_and_complete_task`. It co-commits the memory UPSERT
and the lease-validated ``UPDATE tasks SET status='completed'`` in a single
Postgres transaction, runs FIFO trim on the INSERT branch, skips it on the
UPDATE branch, and rolls back cleanly when the lease is lost.

All scenarios use the isolated test DB on port 55433 — the same one
``make worker-test`` wires up.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timedelta, timezone

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
AGENT_ID = "memory-write-test-agent"
WORKER_A = "memory-write-worker-a"
WORKER_B = "memory-write-worker-b"


async def _scrub(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
        # Order matters — children before parents because of FKs.
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
        pool = await asyncpg.create_pool(DB_DSN, min_size=1, max_size=3)
    except Exception as exc:
        pytest.skip(f"PostgreSQL test database is not available: {exc}")

    await _scrub(pool)
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO agents (tenant_id, agent_id, display_name, agent_config, status)
            VALUES ($1, $2, 'Memory Write Test', '{}'::jsonb, 'active')
            """,
            TENANT_ID, AGENT_ID,
        )
    try:
        yield pool
    finally:
        await _scrub(pool)
        await pool.close()


async def _seed_running_task(
    pool: asyncpg.Pool,
    *,
    task_id: str,
    lease_owner: str = WORKER_A,
    agent_config: dict | None = None,
) -> None:
    agent_config = agent_config or {
        "model": "claude-haiku-4-5",
        "memory": {"enabled": True, "max_entries": 10_000},
    }
    async with pool.acquire() as conn:
        # Snapshot the memory config on the agents row so the API side would
        # see the expected shape too.
        await conn.execute(
            "UPDATE agents SET agent_config = $3::jsonb "
            "WHERE tenant_id = $1 AND agent_id = $2",
            TENANT_ID, AGENT_ID, json.dumps(agent_config),
        )
        await conn.execute(
            """
            INSERT INTO tasks (
                task_id, tenant_id, agent_id, agent_config_snapshot,
                status, input, lease_owner, lease_expiry, version
            ) VALUES ($1::uuid, $2, $3, $4::jsonb, 'running', 'test input',
                      $5, NOW() + INTERVAL '60 seconds', 1)
            """,
            task_id, TENANT_ID, AGENT_ID, json.dumps(agent_config),
            lease_owner,
        )


def _make_executor(pool: asyncpg.Pool, *, worker_id: str = WORKER_A) -> GraphExecutor:
    config = WorkerConfig(worker_id=worker_id)
    return GraphExecutor(config, pool)


def _pending_memory(
    *,
    title: str = "Completed a thing",
    summary: str = "Short summary",
    observations: list[str] | None = None,
    tags: list[str] | None = None,
    content_vec: list[float] | None = None,
    summarizer_model_id: str = "claude-haiku-4-5",
) -> dict:
    return {
        "title": title,
        "summary": summary,
        "outcome": "succeeded",
        "content_vec": content_vec,
        "summarizer_model_id": summarizer_model_id,
        "observations_snapshot": observations or [],
        "tags": tags or [],
        "summarizer_tokens_in": 50,
        "summarizer_tokens_out": 20,
        "summarizer_cost_microdollars": 100,
        "embedding_tokens": 5 if content_vec else 0,
        "embedding_cost_microdollars": 2 if content_vec else 0,
    }


# --------------------------------------------------------------------------


class TestCommitHappyPath:
    @pytest.mark.asyncio
    async def test_insert_branch_writes_memory_row_and_completes_task(
        self, integration_pool: asyncpg.Pool
    ) -> None:
        task_id = str(uuid.uuid4())
        await _seed_running_task(integration_pool, task_id=task_id)
        executor = _make_executor(integration_pool)
        agent_config = {"memory": {"enabled": True, "max_entries": 10_000}}

        pm = _pending_memory(
            title="Completed a thing",
            content_vec=[0.1] * 1536,
            observations=["obs-1"],
        )

        result = await executor._commit_memory_and_complete_task(
            task_id=task_id,
            tenant_id=TENANT_ID,
            agent_id=AGENT_ID,
            pending_memory=pm,
            agent_config=agent_config,
            output={"result": "final answer"},
            worker_id=WORKER_A,
        )

        assert result["committed"] is True
        assert result["inserted"] is True
        assert result["trim_evicted"] == 0

        async with integration_pool.acquire() as conn:
            task_row = await conn.fetchrow(
                "SELECT status, lease_owner, output FROM tasks "
                "WHERE task_id = $1::uuid",
                task_id,
            )
            mem_row = await conn.fetchrow(
                "SELECT title, summary, outcome, observations, tags, "
                "       content_vec IS NOT NULL AS has_vec, "
                "       summarizer_model_id, version, created_at, updated_at "
                "FROM agent_memory_entries WHERE task_id = $1::uuid",
                task_id,
            )

        assert task_row["status"] == "completed"
        assert task_row["lease_owner"] is None
        assert mem_row["title"] == "Completed a thing"
        assert mem_row["outcome"] == "succeeded"
        assert mem_row["has_vec"] is True
        assert mem_row["summarizer_model_id"] == "claude-haiku-4-5"
        assert list(mem_row["observations"]) == ["obs-1"]
        assert list(mem_row["tags"]) == []
        assert mem_row["version"] == 1

    @pytest.mark.asyncio
    async def test_embedding_none_writes_row_with_null_content_vec(
        self, integration_pool: asyncpg.Pool
    ) -> None:
        task_id = str(uuid.uuid4())
        await _seed_running_task(integration_pool, task_id=task_id)
        executor = _make_executor(integration_pool)
        agent_config = {"memory": {"enabled": True, "max_entries": 10_000}}

        pm = _pending_memory(content_vec=None)

        result = await executor._commit_memory_and_complete_task(
            task_id=task_id, tenant_id=TENANT_ID, agent_id=AGENT_ID,
            pending_memory=pm, agent_config=agent_config,
            output={"result": "done"}, worker_id=WORKER_A,
        )

        assert result["committed"] is True
        async with integration_pool.acquire() as conn:
            has_vec = await conn.fetchval(
                "SELECT content_vec IS NOT NULL FROM agent_memory_entries "
                "WHERE task_id = $1::uuid",
                task_id,
            )
        assert has_vec is False

    @pytest.mark.asyncio
    async def test_template_fallback_model_id_allowed(
        self, integration_pool: asyncpg.Pool
    ) -> None:
        task_id = str(uuid.uuid4())
        await _seed_running_task(integration_pool, task_id=task_id)
        executor = _make_executor(integration_pool)
        agent_config = {"memory": {"enabled": True}}

        pm = _pending_memory(summarizer_model_id="template:fallback")
        await executor._commit_memory_and_complete_task(
            task_id=task_id, tenant_id=TENANT_ID, agent_id=AGENT_ID,
            pending_memory=pm, agent_config=agent_config,
            output={"result": "done"}, worker_id=WORKER_A,
        )

        async with integration_pool.acquire() as conn:
            model_id = await conn.fetchval(
                "SELECT summarizer_model_id FROM agent_memory_entries "
                "WHERE task_id = $1::uuid",
                task_id,
            )
        assert model_id == "template:fallback"


class TestCommitUpsertFollowUpBehaviour:
    @pytest.mark.asyncio
    async def test_second_commit_on_same_task_id_updates_and_does_not_trim(
        self, integration_pool: asyncpg.Pool
    ) -> None:
        task_id = str(uuid.uuid4())
        await _seed_running_task(integration_pool, task_id=task_id)
        executor = _make_executor(integration_pool)
        agent_config = {"memory": {"enabled": True, "max_entries": 2}}

        # First commit: INSERT.
        await executor._commit_memory_and_complete_task(
            task_id=task_id, tenant_id=TENANT_ID, agent_id=AGENT_ID,
            pending_memory=_pending_memory(title="First"),
            agent_config=agent_config,
            output={"result": "first"}, worker_id=WORKER_A,
        )

        # Mark the task back to running with a fresh lease so the commit path
        # can complete again — mimics a follow-up / redrive sequence.
        async with integration_pool.acquire() as conn:
            await conn.execute(
                """UPDATE tasks SET status='running', lease_owner=$2,
                          lease_expiry=NOW() + INTERVAL '60 seconds'
                   WHERE task_id=$1::uuid""",
                task_id, WORKER_A,
            )

        result = await executor._commit_memory_and_complete_task(
            task_id=task_id, tenant_id=TENANT_ID, agent_id=AGENT_ID,
            pending_memory=_pending_memory(title="Second", summary="refreshed"),
            agent_config=agent_config,
            output={"result": "second"}, worker_id=WORKER_A,
        )

        assert result["inserted"] is False
        assert result["trim_evicted"] == 0  # UPDATE branch never trims.

        async with integration_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT title, summary, version, created_at, updated_at "
                "FROM agent_memory_entries WHERE task_id = $1::uuid",
                task_id,
            )
        assert row["title"] == "Second"
        assert row["summary"] == "refreshed"
        assert row["version"] == 2
        assert row["updated_at"] > row["created_at"]


class TestCommitTrim:
    @pytest.mark.asyncio
    async def test_trim_fires_when_insert_pushes_past_max_entries(
        self, integration_pool: asyncpg.Pool
    ) -> None:
        # The platform floor on ``max_entries`` is 100 (see design doc
        # "Validation and Consistency Rules"); the worker honours the clamp
        # even if the API shipped a smaller value. Seed 100 pre-existing
        # rows with old timestamps so the INSERT below is the 101st row and
        # trim fires.
        async with integration_pool.acquire() as conn:
            base = datetime.now(timezone.utc) - timedelta(days=2)
            rows = [
                (
                    TENANT_ID, AGENT_ID, str(uuid.uuid4()),
                    f"old-{i:03d}", base + timedelta(minutes=i),
                )
                for i in range(100)
            ]
            await conn.executemany(
                """
                INSERT INTO agent_memory_entries
                    (tenant_id, agent_id, task_id, title, summary,
                     observations, outcome, tags, content_vec,
                     summarizer_model_id, created_at, updated_at)
                VALUES ($1, $2, $3::uuid, $4, 'sum', '{}'::text[],
                        'succeeded', '{}'::text[], NULL,
                        'template:fallback', $5, $5)
                """,
                rows,
            )

        task_id = str(uuid.uuid4())
        await _seed_running_task(integration_pool, task_id=task_id)
        executor = _make_executor(integration_pool)
        # ``max_entries=50`` would be clamped up to 100, so we set exactly 100.
        agent_config = {"memory": {"enabled": True, "max_entries": 100}}

        result = await executor._commit_memory_and_complete_task(
            task_id=task_id, tenant_id=TENANT_ID, agent_id=AGENT_ID,
            pending_memory=_pending_memory(title="new"),
            agent_config=agent_config,
            output={"result": "done"}, worker_id=WORKER_A,
        )
        assert result["inserted"] is True
        # 100 pre-existing + 1 new = 101 > 100 cap → evict exactly 1
        assert result["trim_evicted"] == 1

        async with integration_pool.acquire() as conn:
            total = await conn.fetchval(
                "SELECT COUNT(*) FROM agent_memory_entries "
                "WHERE tenant_id = $1 AND agent_id = $2",
                TENANT_ID, AGENT_ID,
            )
            fresh_row = await conn.fetchrow(
                "SELECT title FROM agent_memory_entries WHERE task_id = $1::uuid",
                task_id,
            )
            # The row with title "old-000" was the oldest → must have been
            # evicted. All newer "old-NNN" rows must survive.
            oldest_still_present = await conn.fetchval(
                "SELECT COUNT(*) FROM agent_memory_entries "
                "WHERE tenant_id = $1 AND agent_id = $2 AND title = 'old-000'",
                TENANT_ID, AGENT_ID,
            )
        assert total == 100
        assert fresh_row is not None
        assert fresh_row["title"] == "new"
        assert oldest_still_present == 0

    @pytest.mark.asyncio
    async def test_trim_does_not_fire_on_update_branch_even_when_over_cap(
        self, integration_pool: asyncpg.Pool
    ) -> None:
        task_id = str(uuid.uuid4())
        await _seed_running_task(integration_pool, task_id=task_id)
        executor = _make_executor(integration_pool)
        agent_config = {"memory": {"enabled": True, "max_entries": 100}}

        # First commit → INSERT with population 1. Below cap → no trim.
        await executor._commit_memory_and_complete_task(
            task_id=task_id, tenant_id=TENANT_ID, agent_id=AGENT_ID,
            pending_memory=_pending_memory(title="initial"),
            agent_config=agent_config,
            output={"result": "a"}, worker_id=WORKER_A,
        )

        # Seed 100 more pre-existing rows so we're at 101 (one over the cap).
        async with integration_pool.acquire() as conn:
            rows = [
                (TENANT_ID, AGENT_ID, str(uuid.uuid4()), f"extra-{i}")
                for i in range(100)
            ]
            await conn.executemany(
                """
                INSERT INTO agent_memory_entries
                    (tenant_id, agent_id, task_id, title, summary,
                     observations, outcome, tags, content_vec,
                     summarizer_model_id)
                VALUES ($1, $2, $3::uuid, $4, 'sum', '{}'::text[],
                        'succeeded', '{}'::text[], NULL,
                        'template:fallback')
                """,
                rows,
            )
            # Reset task lease for the follow-up commit.
            await conn.execute(
                """UPDATE tasks SET status='running', lease_owner=$2,
                          lease_expiry=NOW() + INTERVAL '60 seconds'
                   WHERE task_id=$1::uuid""",
                task_id, WORKER_A,
            )

        # Follow-up: the UPSERT takes the UPDATE branch. Even though the
        # population is 101 (above the 100 cap), the UPDATE branch MUST NOT
        # trigger trim — row count is unchanged by the UPDATE itself.
        result = await executor._commit_memory_and_complete_task(
            task_id=task_id, tenant_id=TENANT_ID, agent_id=AGENT_ID,
            pending_memory=_pending_memory(title="updated"),
            agent_config=agent_config,
            output={"result": "b"}, worker_id=WORKER_A,
        )
        assert result["inserted"] is False
        assert result["trim_evicted"] == 0

        async with integration_pool.acquire() as conn:
            total = await conn.fetchval(
                "SELECT COUNT(*) FROM agent_memory_entries "
                "WHERE tenant_id = $1 AND agent_id = $2",
                TENANT_ID, AGENT_ID,
            )
        # 1 updated + 100 extras = 101 (unchanged by the UPDATE; no trim).
        assert total == 101


class TestCommitLeaseEnforcement:
    @pytest.mark.asyncio
    async def test_lease_revoked_rolls_back_memory_row_and_task_update(
        self, integration_pool: asyncpg.Pool
    ) -> None:
        task_id = str(uuid.uuid4())
        # Seed the task under WORKER_A so WORKER_B cannot commit.
        await _seed_running_task(integration_pool, task_id=task_id, lease_owner=WORKER_A)
        executor = _make_executor(integration_pool, worker_id=WORKER_B)
        agent_config = {"memory": {"enabled": True}}

        with pytest.raises(LeaseRevokedException):
            await executor._commit_memory_and_complete_task(
                task_id=task_id, tenant_id=TENANT_ID, agent_id=AGENT_ID,
                pending_memory=_pending_memory(),
                agent_config=agent_config,
                output={"result": "lost"}, worker_id=WORKER_B,
            )

        async with integration_pool.acquire() as conn:
            task_row = await conn.fetchrow(
                "SELECT status, lease_owner FROM tasks WHERE task_id = $1::uuid",
                task_id,
            )
            mem_count = await conn.fetchval(
                "SELECT COUNT(*) FROM agent_memory_entries WHERE task_id = $1::uuid",
                task_id,
            )
        # Task is still running under WORKER_A; no memory row leaked.
        assert task_row["status"] == "running"
        assert task_row["lease_owner"] == WORKER_A
        assert mem_count == 0


class TestCommitMissingPendingMemorySafetyNet:
    @pytest.mark.asyncio
    async def test_none_pending_memory_still_completes_task(
        self, integration_pool: asyncpg.Pool
    ) -> None:
        """Safety net: ``pending_memory`` must not be ``None`` on the success
        path, but the commit path MUST still complete the task if it ever is.
        """
        task_id = str(uuid.uuid4())
        await _seed_running_task(integration_pool, task_id=task_id)
        executor = _make_executor(integration_pool)
        agent_config = {"memory": {"enabled": True}}

        result = await executor._commit_memory_and_complete_task(
            task_id=task_id, tenant_id=TENANT_ID, agent_id=AGENT_ID,
            pending_memory=None, agent_config=agent_config,
            output={"result": "ok"}, worker_id=WORKER_A,
        )
        assert result["committed"] is True
        assert result["memory_written"] is False

        async with integration_pool.acquire() as conn:
            status = await conn.fetchval(
                "SELECT status FROM tasks WHERE task_id = $1::uuid", task_id
            )
            count = await conn.fetchval(
                "SELECT COUNT(*) FROM agent_memory_entries WHERE task_id = $1::uuid",
                task_id,
            )
        assert status == "completed"
        assert count == 0
