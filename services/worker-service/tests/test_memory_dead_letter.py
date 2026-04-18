"""Integration tests for the Phase 2 Track 5 Task 8 dead-letter memory hook.

Covers :py:meth:`GraphExecutor._handle_dead_letter` memory branch:

* ``cancelled_by_user`` → nothing written, regardless of observations.
* Genuine failure, no observations → nothing written.
* Genuine failure, with observations → template entry
  (``outcome='failed'``, ``summarizer_model_id='template:dead_letter'``),
  observations preserved verbatim, ``tags=[]``.
* Lease revoked mid-transaction → entire transaction rolls back, no memory
  row AND no dead-letter status change.
* Embedding provider down → row written with ``content_vec=NULL``, task
  still transitions to ``dead_letter``.

All scenarios use the isolated test DB on port 55433 (``make worker-test``).
"""

from __future__ import annotations

import json
import os
import uuid
from typing import Any

import asyncpg
import pytest

import executor.graph as graph_module
from checkpointer.postgres import PostgresDurableCheckpointer
from core.config import WorkerConfig
from executor.graph import GraphExecutor


DB_DSN = os.getenv(
    "E2E_DB_DSN",
    "postgresql://postgres:postgres@localhost:55433/persistent_agent_runtime_e2e",
)

TENANT_ID = "default"
AGENT_ID = "memory-dead-letter-test-agent"
WORKER_A = "memory-dl-worker-a"
WORKER_B = "memory-dl-worker-b"


async def _scrub(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
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
            VALUES ($1, $2, 'Memory DL Test', '{}'::jsonb, 'active')
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
    agent_config: dict[str, Any] | None = None,
    retry_count: int = 0,
) -> None:
    agent_config = agent_config or {
        "model": "claude-haiku-4-5",
        "memory": {"enabled": True, "max_entries": 10_000},
    }
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE agents SET agent_config = $3::jsonb "
            "WHERE tenant_id = $1 AND agent_id = $2",
            TENANT_ID, AGENT_ID, json.dumps(agent_config),
        )
        await conn.execute(
            """
            INSERT INTO tasks (
                task_id, tenant_id, agent_id, agent_config_snapshot,
                status, input, lease_owner, lease_expiry, version, retry_count
            ) VALUES ($1::uuid, $2, $3, $4::jsonb, 'running', 'test input',
                      $5, NOW() + INTERVAL '60 seconds', 1, $6)
            """,
            task_id, TENANT_ID, AGENT_ID, json.dumps(agent_config),
            lease_owner, retry_count,
        )


async def _seed_checkpoint_with_observations(
    pool: asyncpg.Pool,
    *,
    task_id: str,
    observations: list[str],
) -> None:
    """Insert a checkpoint row with the given ``observations`` in
    ``channel_values``. The dead-letter hook uses ``aget_tuple`` to read
    this value.
    """
    import time as _time
    checkpoint_id = f"1{_time.monotonic_ns()}"
    payload = {
        "v": 1,
        "ts": "2026-01-01T00:00:00+00:00",
        "id": checkpoint_id,
        "channel_values": {
            "messages": [],
            "observations": list(observations),
            "pending_memory": None,
        },
        "channel_versions": {},
        "versions_seen": {},
    }
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO checkpoints (
                task_id, checkpoint_ns, checkpoint_id, worker_id,
                parent_checkpoint_id, thread_ts, parent_ts,
                checkpoint_payload, metadata_payload
            ) VALUES (
                $1::uuid, '', $2, $3, NULL, $2, NULL,
                $4::jsonb, '{}'::jsonb
            )
            """,
            task_id, checkpoint_id, WORKER_A, json.dumps(payload),
        )


def _make_executor(pool: asyncpg.Pool, *, worker_id: str = WORKER_A) -> GraphExecutor:
    config = WorkerConfig(worker_id=worker_id)
    return GraphExecutor(config, pool)


def _make_checkpointer(pool: asyncpg.Pool) -> PostgresDurableCheckpointer:
    return PostgresDurableCheckpointer(pool, worker_id=WORKER_A, tenant_id=TENANT_ID)


# ---------------------------------------------------------------------------


class TestCancelledByUserSkipsMemoryWrite:
    @pytest.mark.asyncio
    async def test_cancelled_by_user_writes_nothing_even_with_observations(
        self, integration_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Embedding provider must never be reached on the skip path; fail
        # loudly if the hook accidentally tries.
        async def _boom(*args, **kwargs):
            raise AssertionError("embedding should not be called on cancel path")
        monkeypatch.setattr(graph_module, "_default_compute_embedding", _boom)

        task_id = str(uuid.uuid4())
        await _seed_running_task(integration_pool, task_id=task_id)
        await _seed_checkpoint_with_observations(
            integration_pool, task_id=task_id,
            observations=["obs-1", "obs-2"],
        )

        executor = _make_executor(integration_pool)
        checkpointer = _make_checkpointer(integration_pool)
        agent_config = {"memory": {"enabled": True, "max_entries": 10_000}}

        await executor._handle_dead_letter(
            task_id, TENANT_ID, AGENT_ID,
            "cancelled_by_user",
            "customer aborted task",
            memory_enabled=True,
            agent_config=agent_config,
            task_input="test input",
            retry_count=0,
            checkpointer=checkpointer,
        )

        async with integration_pool.acquire() as conn:
            count = await conn.fetchval(
                "SELECT COUNT(*) FROM agent_memory_entries "
                "WHERE tenant_id = $1 AND agent_id = $2",
                TENANT_ID, AGENT_ID,
            )
            row = await conn.fetchrow(
                "SELECT status, dead_letter_reason FROM tasks "
                "WHERE task_id = $1::uuid",
                task_id,
            )

        assert count == 0
        assert row["status"] == "dead_letter"
        assert row["dead_letter_reason"] == "cancelled_by_user"


class TestNoObservationsSkipsMemoryWrite:
    @pytest.mark.asyncio
    async def test_genuine_failure_without_observations_writes_nothing(
        self, integration_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        async def _boom(*args, **kwargs):
            raise AssertionError("embedding should not be called when obs empty")
        monkeypatch.setattr(graph_module, "_default_compute_embedding", _boom)

        task_id = str(uuid.uuid4())
        await _seed_running_task(integration_pool, task_id=task_id)
        # Deliberately NO observations checkpoint seeded.
        executor = _make_executor(integration_pool)
        checkpointer = _make_checkpointer(integration_pool)
        agent_config = {"memory": {"enabled": True}}

        await executor._handle_dead_letter(
            task_id, TENANT_ID, AGENT_ID,
            "non_retryable_error",
            "oh no",
            error_code="fatal_error",
            memory_enabled=True,
            agent_config=agent_config,
            task_input="test input",
            retry_count=0,
            checkpointer=checkpointer,
        )

        async with integration_pool.acquire() as conn:
            count = await conn.fetchval(
                "SELECT COUNT(*) FROM agent_memory_entries "
                "WHERE tenant_id = $1 AND agent_id = $2",
                TENANT_ID, AGENT_ID,
            )
            status = await conn.fetchval(
                "SELECT status FROM tasks WHERE task_id = $1::uuid", task_id,
            )
        assert count == 0
        assert status == "dead_letter"


class TestGenuineFailureWithObservations:
    @pytest.mark.asyncio
    async def test_template_entry_written_with_expected_fields(
        self, integration_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Stub embedding provider — return a valid 1536-d vector so the row
        # carries a non-null content_vec and the cost-ledger branch fires.
        from executor.embeddings import EmbeddingResult

        async def _fake_embed(text, *, pool=None):
            return EmbeddingResult(
                vector=[0.01] * 1536,
                tokens=10,
                cost_microdollars=3,
            )
        monkeypatch.setattr(graph_module, "_default_compute_embedding", _fake_embed)

        task_id = str(uuid.uuid4())
        await _seed_running_task(
            integration_pool, task_id=task_id, retry_count=2,
        )
        await _seed_checkpoint_with_observations(
            integration_pool, task_id=task_id,
            observations=["obs-a", "obs-b", "obs-c"],
        )

        executor = _make_executor(integration_pool)
        checkpointer = _make_checkpointer(integration_pool)
        agent_config = {"memory": {"enabled": True, "max_entries": 10_000}}

        await executor._handle_dead_letter(
            task_id, TENANT_ID, AGENT_ID,
            "retries_exhausted",
            "Max retries reached. Last error: boom",
            memory_enabled=True,
            agent_config=agent_config,
            task_input="my task description",
            retry_count=2,
            checkpointer=checkpointer,
        )

        async with integration_pool.acquire() as conn:
            mem = await conn.fetchrow(
                """SELECT title, summary, outcome, observations, tags,
                          summarizer_model_id,
                          content_vec IS NOT NULL AS has_vec
                   FROM agent_memory_entries
                   WHERE tenant_id = $1 AND agent_id = $2
                     AND task_id = $3::uuid""",
                TENANT_ID, AGENT_ID, task_id,
            )
            task_row = await conn.fetchrow(
                "SELECT status, dead_letter_reason FROM tasks "
                "WHERE task_id = $1::uuid", task_id,
            )

        assert mem is not None
        assert mem["outcome"] == "failed"
        assert mem["summarizer_model_id"] == "template:dead_letter"
        assert list(mem["observations"]) == ["obs-a", "obs-b", "obs-c"]
        assert list(mem["tags"]) == []
        assert mem["title"].startswith("[Failed]")
        assert "retries_exhausted" in mem["summary"] or "2 retries" in mem["summary"]
        assert mem["has_vec"] is True
        assert task_row["status"] == "dead_letter"
        assert task_row["dead_letter_reason"] == "retries_exhausted"


class TestLeaseRevokedRollsBackMemory:
    @pytest.mark.asyncio
    async def test_lease_mismatch_rolls_back_memory_and_status(
        self, integration_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from executor.embeddings import EmbeddingResult

        async def _fake_embed(text, *, pool=None):
            return EmbeddingResult(
                vector=[0.01] * 1536, tokens=1, cost_microdollars=1,
            )
        monkeypatch.setattr(graph_module, "_default_compute_embedding", _fake_embed)

        task_id = str(uuid.uuid4())
        # Task leased by WORKER_A; dead-letter attempted by WORKER_B → tx
        # must roll back entirely.
        await _seed_running_task(
            integration_pool, task_id=task_id, lease_owner=WORKER_A,
        )
        await _seed_checkpoint_with_observations(
            integration_pool, task_id=task_id,
            observations=["only-obs"],
        )

        executor = _make_executor(integration_pool, worker_id=WORKER_B)
        checkpointer = _make_checkpointer(integration_pool)
        agent_config = {"memory": {"enabled": True}}

        # Lease loss inside the hook rolls back the whole tx and logs a
        # warning — the method returns cleanly (same log-and-return
        # semantics as pre-Task-8 dead-letter callers depend on).
        await executor._handle_dead_letter(
            task_id, TENANT_ID, AGENT_ID,
            "non_retryable_error", "boom",
            error_code="fatal_error",
            memory_enabled=True,
            agent_config=agent_config,
            task_input="x",
            retry_count=0,
            checkpointer=checkpointer,
        )

        async with integration_pool.acquire() as conn:
            mem_count = await conn.fetchval(
                "SELECT COUNT(*) FROM agent_memory_entries "
                "WHERE task_id = $1::uuid", task_id,
            )
            task_row = await conn.fetchrow(
                "SELECT status, lease_owner FROM tasks "
                "WHERE task_id = $1::uuid", task_id,
            )
        # Memory row rolled back atomically.
        assert mem_count == 0
        # Task remained running under the original lease owner.
        assert task_row["status"] == "running"
        assert task_row["lease_owner"] == WORKER_A


class TestEmbeddingDownWritesNullVector:
    @pytest.mark.asyncio
    async def test_embedding_returns_none_row_still_written(
        self, integration_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        async def _no_embed(text, *, pool=None):
            return None
        monkeypatch.setattr(graph_module, "_default_compute_embedding", _no_embed)

        task_id = str(uuid.uuid4())
        await _seed_running_task(integration_pool, task_id=task_id)
        await _seed_checkpoint_with_observations(
            integration_pool, task_id=task_id,
            observations=["one-obs"],
        )

        executor = _make_executor(integration_pool)
        checkpointer = _make_checkpointer(integration_pool)
        agent_config = {"memory": {"enabled": True}}

        await executor._handle_dead_letter(
            task_id, TENANT_ID, AGENT_ID,
            "task_timeout", "timed out",
            memory_enabled=True,
            agent_config=agent_config,
            task_input="x",
            retry_count=0,
            checkpointer=checkpointer,
        )

        async with integration_pool.acquire() as conn:
            row = await conn.fetchrow(
                """SELECT outcome, summarizer_model_id,
                          content_vec IS NULL AS vec_null
                   FROM agent_memory_entries
                   WHERE task_id = $1::uuid""",
                task_id,
            )
            status = await conn.fetchval(
                "SELECT status FROM tasks WHERE task_id = $1::uuid", task_id,
            )
        assert row is not None
        assert row["vec_null"] is True
        assert row["outcome"] == "failed"
        assert row["summarizer_model_id"] == "template:dead_letter"
        assert status == "dead_letter"


class TestMemoryDisabledSkipsWrite:
    @pytest.mark.asyncio
    async def test_memory_disabled_no_row_written(
        self, integration_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        async def _boom(*args, **kwargs):
            raise AssertionError("embedding must not be called when memory disabled")
        monkeypatch.setattr(graph_module, "_default_compute_embedding", _boom)

        task_id = str(uuid.uuid4())
        await _seed_running_task(integration_pool, task_id=task_id)
        await _seed_checkpoint_with_observations(
            integration_pool, task_id=task_id, observations=["obs"],
        )

        executor = _make_executor(integration_pool)
        checkpointer = _make_checkpointer(integration_pool)

        await executor._handle_dead_letter(
            task_id, TENANT_ID, AGENT_ID,
            "non_retryable_error", "boom",
            error_code="fatal_error",
            memory_enabled=False,  # ← gating disabled
            agent_config={"memory": {"enabled": False}},
            task_input="x",
            retry_count=0,
            checkpointer=checkpointer,
        )

        async with integration_pool.acquire() as conn:
            count = await conn.fetchval(
                "SELECT COUNT(*) FROM agent_memory_entries "
                "WHERE task_id = $1::uuid", task_id,
            )
            status = await conn.fetchval(
                "SELECT status FROM tasks WHERE task_id = $1::uuid", task_id,
            )
        assert count == 0
        assert status == "dead_letter"
