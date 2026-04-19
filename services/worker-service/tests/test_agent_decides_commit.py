"""Phase 2 Track 5 Task 12 — ``agent_decides`` commit-path integration.

Drives :meth:`GraphExecutor.execute_task` end-to-end with a stubbed
compiled graph so the real post-commit gate (new at Task 12) is exercised
against a live Postgres instance:

* ``memory_mode='agent_decides'`` + final state ``memory_opt_in=False`` →
  no memory row, no dead-letter, no cost ledger row for the summarizer.
* ``memory_mode='agent_decides'`` + final state ``memory_opt_in=True`` →
  memory row written, reason visible in the observations snapshot,
  summarizer cost ledger row present.
* Per-run reset: two consecutive runs under the same ``agent_decides``
  config; run 1 opts in and writes, run 2 (seeded with no opt-in in the
  final state, as it would be after a per-run reset) does NOT write.

The stub surface mirrors ``test_budget_carve_out_end_to_end.py``: only the
LangGraph compile/astream layer is faked. DB, cost ledger, budget check,
commit transaction, and lease validation all run against the real
``par-e2e-postgres`` instance on port 55433.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import asyncpg
import pytest

from core.config import WorkerConfig
from executor.graph import GraphExecutor


DB_DSN = os.getenv(
    "E2E_DB_DSN",
    "postgresql://postgres:postgres@localhost:55433/persistent_agent_runtime_e2e",
)

TENANT_ID = "default"
AGENT_ID = "agent-decides-commit-test"
WORKER_ID = "agent-decides-commit-worker"


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
            INSERT INTO agents (
                tenant_id, agent_id, display_name, agent_config, status
            )
            VALUES ($1, $2, 'Agent Decides Test', '{}'::jsonb, 'active')
            """,
            TENANT_ID, AGENT_ID,
        )
    try:
        yield pool
    finally:
        await _scrub(pool)
        await pool.close()


def _agent_config() -> dict:
    return {
        "model": "claude-haiku-4-5",
        "allowed_tools": [],
        "memory": {"enabled": True, "max_entries": 10_000},
    }


async def _seed_running_task(
    pool: asyncpg.Pool, *, task_id: str, memory_mode: str
) -> dict:
    cfg = _agent_config()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE agents SET agent_config = $3::jsonb "
            "WHERE tenant_id = $1 AND agent_id = $2",
            TENANT_ID, AGENT_ID, json.dumps(cfg),
        )
        await conn.execute(
            """
            INSERT INTO tasks (
                task_id, tenant_id, agent_id, agent_config_snapshot,
                status, input, lease_owner, lease_expiry, version
            )
            VALUES ($1::uuid, $2, $3, $4::jsonb, 'running', 'go',
                    $5, NOW() + INTERVAL '120 seconds', 1)
            """,
            task_id, TENANT_ID, AGENT_ID,
            json.dumps(cfg), WORKER_ID,
        )
    return {
        "task_id": task_id,
        "tenant_id": TENANT_ID,
        "agent_id": AGENT_ID,
        "agent_config_snapshot": json.dumps(cfg),
        "input": "go",
        "max_steps": 10,
        "task_timeout_seconds": 60,
        "retry_count": 0,
        "max_retries": 3,
        "memory_mode": memory_mode,
    }


async def _seed_checkpoint(pool: asyncpg.Pool, task_id: str) -> str:
    """Seed a checkpoint row so the commit path has a checkpoint_id for
    summarizer-cost attribution."""
    checkpoint_id = "cp-agent-decides"
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO checkpoints (
                task_id, checkpoint_ns, checkpoint_id, worker_id, thread_ts,
                checkpoint_payload, metadata_payload
            )
            VALUES ($1::uuid, '', $2, $3, '2026-04-17T00:00:00Z',
                    '{}'::jsonb, '{}'::jsonb)
            """,
            task_id, checkpoint_id, WORKER_ID,
        )
    return checkpoint_id


def _pending_memory_with_save_reason(reason: str) -> dict:
    return {
        "title": "Completed with opt-in",
        "summary": "short summary",
        "outcome": "succeeded",
        "content_vec": None,
        "summarizer_model_id": "claude-haiku-4-5",
        "observations_snapshot": [f"[save_memory] {reason}"],
        "tags": [],
        "summarizer_tokens_in": 50,
        "summarizer_tokens_out": 20,
        "summarizer_cost_microdollars": 100,
        "embedding_tokens": 0,
        "embedding_cost_microdollars": 0,
    }


def _stub_compiled_graph(*, events: list[dict], final_state_values: dict):
    compiled = MagicMock()

    async def astream(*args, **kwargs):
        for ev in events:
            yield ev

    compiled.astream = astream
    compiled.aget_state = AsyncMock(
        return_value=MagicMock(values=final_state_values, tasks=[])
    )
    return compiled


class _StubCheckpointer:
    async def aget_tuple(self, _config):
        return None


async def _run_task(executor: GraphExecutor, task_data: dict, compiled) -> None:
    fake_graph = MagicMock()
    fake_graph.compile.return_value = compiled
    with patch.object(
        executor, "_build_graph", AsyncMock(return_value=fake_graph)
    ), patch(
        "executor.graph.PostgresDurableCheckpointer",
        return_value=_StubCheckpointer(),
    ):
        cancel_event = asyncio.Event()
        await executor.execute_task(task_data, cancel_event)


# ---------------------------------------------------------------------------


class TestAgentDecidesNoOptIn:
    @pytest.mark.asyncio
    async def test_no_save_memory_call_skips_commit(
        self, integration_pool: asyncpg.Pool
    ) -> None:
        """`agent_decides` + never-opt-in → task completes without a memory
        row, without a dead-letter row, and without summarizer cost on the
        ledger (the ``memory_write`` node never fires → commit path never
        runs)."""
        task_id = str(uuid.uuid4())
        task_data = await _seed_running_task(
            integration_pool, task_id=task_id, memory_mode="agent_decides"
        )
        await _seed_checkpoint(integration_pool, task_id)

        # No ``memory_write`` event — the agent terminated without opting in.
        events: list[dict] = []
        final_state_values = {
            "messages": [],
            "observations": [],
            "memory_opt_in": False,
        }

        executor = GraphExecutor(
            WorkerConfig(worker_id=WORKER_ID, tenant_id=TENANT_ID),
            integration_pool,
        )
        await _run_task(
            executor,
            task_data,
            _stub_compiled_graph(
                events=events, final_state_values=final_state_values
            ),
        )

        async with integration_pool.acquire() as conn:
            task_row = await conn.fetchrow(
                "SELECT status FROM tasks WHERE task_id = $1::uuid", task_id,
            )
            mem_count = await conn.fetchval(
                "SELECT COUNT(*) FROM agent_memory_entries "
                "WHERE task_id = $1::uuid", task_id,
            )
            ledger_count = await conn.fetchval(
                "SELECT COUNT(*) FROM agent_cost_ledger "
                "WHERE task_id = $1::uuid", task_id,
            )

        assert task_row["status"] == "completed", (
            "agent_decides + no-opt should complete the task cleanly"
        )
        assert mem_count == 0, (
            "no memory row should be written when memory_opt_in is False"
        )
        assert ledger_count == 0, (
            "summarizer cost should not be charged when opt-in is absent"
        )


class TestAgentDecidesOptInCommit:
    @pytest.mark.asyncio
    async def test_save_memory_opt_in_writes_row_and_reason_observation(
        self, integration_pool: asyncpg.Pool
    ) -> None:
        """`agent_decides` + opt-in → same commit path as ``always`` mode,
        with the ``[save_memory] <reason>`` observation preserved verbatim
        and the summarizer cost written to the ledger."""
        task_id = str(uuid.uuid4())
        task_data = await _seed_running_task(
            integration_pool, task_id=task_id, memory_mode="agent_decides"
        )
        await _seed_checkpoint(integration_pool, task_id)

        pending = _pending_memory_with_save_reason("worth remembering")
        events = [
            {
                "memory_write": {
                    "pending_memory": pending,
                    "messages": [],
                }
            }
        ]
        final_state_values = {
            "messages": [],
            "observations": ["[save_memory] worth remembering"],
            "pending_memory": pending,
            "memory_opt_in": True,
        }

        executor = GraphExecutor(
            WorkerConfig(worker_id=WORKER_ID, tenant_id=TENANT_ID),
            integration_pool,
        )
        await _run_task(
            executor,
            task_data,
            _stub_compiled_graph(
                events=events, final_state_values=final_state_values
            ),
        )

        async with integration_pool.acquire() as conn:
            task_row = await conn.fetchrow(
                "SELECT status FROM tasks WHERE task_id = $1::uuid", task_id,
            )
            mem_row = await conn.fetchrow(
                "SELECT summarizer_model_id, observations, outcome "
                "FROM agent_memory_entries WHERE task_id = $1::uuid",
                task_id,
            )
            ledger_total = await conn.fetchval(
                "SELECT COALESCE(SUM(cost_microdollars), 0) "
                "FROM agent_cost_ledger WHERE task_id = $1::uuid",
                task_id,
            )

        assert task_row["status"] == "completed"
        assert mem_row is not None, (
            "agent_decides + opt-in should write a memory row"
        )
        assert mem_row["outcome"] == "succeeded"
        assert "[save_memory] worth remembering" in list(mem_row["observations"])
        assert int(ledger_total) >= 100, (
            "summarizer cost should be recorded in the cost ledger"
        )


class TestAgentDecidesPerRunReset:
    @pytest.mark.asyncio
    async def test_second_run_without_opt_in_writes_no_second_memory(
        self, integration_pool: asyncpg.Pool
    ) -> None:
        """Run 1 opts in and writes memory; run 2 under the same mode with
        ``memory_opt_in=False`` in its final state must NOT write a second
        memory. The per-run reset invariant means each run earns its opt-in
        from scratch.
        """
        task_id = str(uuid.uuid4())
        task_data = await _seed_running_task(
            integration_pool, task_id=task_id, memory_mode="agent_decides"
        )
        await _seed_checkpoint(integration_pool, task_id)

        # Run 1 — opts in.
        pending = _pending_memory_with_save_reason("first run opt-in")
        events_run1 = [
            {"memory_write": {"pending_memory": pending, "messages": []}}
        ]
        final_state_run1 = {
            "messages": [],
            "observations": ["[save_memory] first run opt-in"],
            "pending_memory": pending,
            "memory_opt_in": True,
        }
        executor = GraphExecutor(
            WorkerConfig(worker_id=WORKER_ID, tenant_id=TENANT_ID),
            integration_pool,
        )
        await _run_task(
            executor,
            task_data,
            _stub_compiled_graph(
                events=events_run1, final_state_values=final_state_run1
            ),
        )

        async with integration_pool.acquire() as conn:
            count_after_run1 = await conn.fetchval(
                "SELECT COUNT(*) FROM agent_memory_entries "
                "WHERE task_id = $1::uuid", task_id,
            )
            updated_at_run1 = await conn.fetchval(
                "SELECT updated_at FROM agent_memory_entries "
                "WHERE task_id = $1::uuid", task_id,
            )
        assert count_after_run1 == 1, "run 1 should have written one row"

        # Reset the task row so run 2 can execute under the same task_id as a
        # follow-up / redrive. The persisted memory row stays intact; the
        # per-run reset lives in the in-memory state, so the fact that run 2
        # ends with ``memory_opt_in=False`` is the important bit.
        async with integration_pool.acquire() as conn:
            await conn.execute(
                "UPDATE tasks SET status = 'running', lease_owner = $2, "
                "lease_expiry = NOW() + INTERVAL '120 seconds' "
                "WHERE task_id = $1::uuid",
                task_id, WORKER_ID,
            )

        # Run 2 — no opt-in. No ``memory_write`` event emitted, final state
        # has ``memory_opt_in=False`` (the per-run reset is honoured at
        # initial-state seeding by :meth:`execute_task`).
        events_run2: list[dict] = []
        final_state_run2 = {
            "messages": [],
            "observations": [],
            "memory_opt_in": False,
        }
        task_data_run2 = dict(task_data)
        await _run_task(
            executor,
            task_data_run2,
            _stub_compiled_graph(
                events=events_run2, final_state_values=final_state_run2
            ),
        )

        async with integration_pool.acquire() as conn:
            count_after_run2 = await conn.fetchval(
                "SELECT COUNT(*) FROM agent_memory_entries "
                "WHERE task_id = $1::uuid", task_id,
            )
            updated_at_run2 = await conn.fetchval(
                "SELECT updated_at FROM agent_memory_entries "
                "WHERE task_id = $1::uuid", task_id,
            )
        # Still exactly one memory row (the one from run 1). Run 2 did not
        # overwrite / upsert it.
        assert count_after_run2 == 1, (
            f"run 2 without opt-in must not write a second memory row; "
            f"got {count_after_run2}"
        )
        # Row untouched (no update to updated_at).
        assert updated_at_run2 == updated_at_run1, (
            "run 2 without opt-in must not touch the existing memory row"
        )
