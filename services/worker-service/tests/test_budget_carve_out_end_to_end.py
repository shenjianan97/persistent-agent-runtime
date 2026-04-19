"""End-to-end budget carve-out for the ``memory_write`` super-step.

Covers AC-14 (design doc § "Acceptance Criteria" #14): summarizer LLM cost is
exempt from ``budget_max_per_task`` pause enforcement (hourly-window spend
still accrues). The existing unit test ``test_memory_budget_carve_out.py``
calls ``_commit_memory_and_complete_task`` directly, which means the actual
code path the reviewer flagged — the explicit carve-out branch

    if MEMORY_WRITE_NODE_NAME in event:
        continue

in ``executor/graph.py`` around line 1700 — is never exercised. If the
carve-out were deleted, the unit test would still pass (because the commit
path never flowed through the budget check in the first place).

This file closes that gap by driving :meth:`GraphExecutor.execute_task` with
a stubbed compiled graph that yields events keyed by node name, and leaving
the real DB, real ``_check_budget_and_pause``, real ``_record_step_cost``,
and real ``_commit_memory_and_complete_task`` paths untouched. The stubbed
surface is the LangGraph compile/astream layer only. That is the narrowest
stub that still exercises the budget-loop code path end-to-end.

Two scenarios:

1. **Carve-out branch:** the astream stream emits a ``memory_write`` event
   whose summarizer cost alone would exceed the per-task cap. Expected:
   task status = ``completed``, NOT ``paused``; summarizer cost IS written
   to ``agent_cost_ledger`` and accumulated in the hourly-window aggregator
   by the commit path.

2. **Control (non-carve-out):** the astream stream emits an ``agent``
   event whose chat-model cost exceeds the per-task cap. Expected: task
   status = ``paused`` with ``pause_reason = 'budget_per_task'``. This
   proves the budget-loop itself still fires for non-memory super-steps —
   i.e. the carve-out is a narrow exception, not a global disablement of
   per-step enforcement.
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
AGENT_ID = "budget-carve-out-e2e-agent"
WORKER_ID = "budget-carve-out-e2e-worker"


# ---------------------------------------------------------------------------
# DB lifecycle
# ---------------------------------------------------------------------------


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
    # Seed agent with tight per-task budget — 1_000 microdollars. The
    # summarizer + agent costs below are both 5_000 microdollars (5x the
    # cap) so budget enforcement would fire unconditionally without the
    # carve-out.
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO agents (tenant_id, agent_id, display_name, agent_config,
                                status, budget_max_per_task, budget_max_per_hour)
            VALUES ($1, $2, 'Budget E2E', '{}'::jsonb, 'active', 1000, 10000000)
            """,
            TENANT_ID, AGENT_ID,
        )
    try:
        yield pool
    finally:
        await _scrub(pool)
        await pool.close()


def _agent_config(*, memory_enabled: bool = True) -> dict:
    return {
        "model": "claude-haiku-4-5",
        "allowed_tools": [],
        "memory": (
            {"enabled": True, "max_entries": 10_000}
            if memory_enabled
            else {"enabled": False}
        ),
    }


async def _seed_running_task(
    pool: asyncpg.Pool,
    *,
    task_id: str,
    memory_enabled: bool = True,
) -> dict:
    cfg = _agent_config(memory_enabled=memory_enabled)
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE agents SET agent_config = $3::jsonb "
            "WHERE tenant_id = $1 AND agent_id = $2",
            TENANT_ID, AGENT_ID, json.dumps(cfg),
        )
        await conn.execute(
            """
            INSERT INTO tasks (task_id, tenant_id, agent_id,
                               agent_config_snapshot, status, input,
                               lease_owner, lease_expiry, version)
            VALUES ($1::uuid, $2, $3, $4::jsonb, 'running', 'go',
                    $5, NOW() + INTERVAL '120 seconds', 1)
            """,
            task_id, TENANT_ID, AGENT_ID,
            json.dumps(cfg), WORKER_ID,
        )
    # Return a task_data dict of the same shape the worker router passes
    # into ``execute_task``.
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
        "memory_mode": "always",
    }


async def _seed_checkpoint(pool: asyncpg.Pool, task_id: str) -> str:
    """Seed a checkpoint row so the commit path and the per-step cost path
    have a ``checkpoint_id`` to attribute cost against (both production
    paths ``SELECT ... FROM checkpoints ... ORDER BY created_at DESC LIMIT 1``
    so we don't need per-step checkpoint churn).
    """
    checkpoint_id = "cp-e2e"
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO checkpoints (task_id, checkpoint_ns, checkpoint_id,
                                     worker_id, thread_ts, checkpoint_payload,
                                     metadata_payload)
            VALUES ($1::uuid, '', $2, $3, '2026-04-17T00:00:00Z',
                    '{}'::jsonb, '{}'::jsonb)
            """,
            task_id, checkpoint_id, WORKER_ID,
        )
    return checkpoint_id


# ---------------------------------------------------------------------------
# Stubbed compiled-graph fixture — the only layer we replace. Everything
# underneath (DB, cost ledger, budget pause, commit transaction) is real.
# ---------------------------------------------------------------------------


def _make_ai_msg_with_cost(*, content: str, input_tokens: int, output_tokens: int):
    """Build an AIMessage-like object the ``agent`` event-handler branch in
    ``execute_task`` consumes for per-step cost attribution. Production reads
    ``response_metadata`` / ``usage_metadata`` via ``_extract_tokens``.
    """
    msg = MagicMock()
    msg.type = "ai"
    msg.content = content
    msg.response_metadata = {
        "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens}
    }
    msg.usage_metadata = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
    }
    msg.tool_calls = []
    return msg


def _stub_compiled_graph(*, events: list[dict], final_state_values: dict):
    """Build a MagicMock that looks like a compiled LangGraph for the purposes
    of ``execute_task``'s streaming loop. Only the methods the executor
    actually calls are stubbed.
    """
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
    """Minimal async-API stand-in for :class:`PostgresDurableCheckpointer`.
    Only needs ``aget_tuple`` for the first-run detection branch."""

    async def aget_tuple(self, _config):
        return None


# ---------------------------------------------------------------------------
# Scenario 1: memory_write event does NOT pause even when cost blows budget.
# ---------------------------------------------------------------------------


class TestMemoryWriteEventCarveOut:
    @pytest.mark.asyncio
    async def test_memory_write_event_over_budget_does_not_pause_task(
        self, integration_pool: asyncpg.Pool
    ) -> None:
        # Covers AC-14 carve-out branch: an event keyed by ``memory_write``
        # hits the ``if MEMORY_WRITE_NODE_NAME in event: continue`` short-
        # circuit in ``execute_task``'s streaming loop. Summarizer cost is
        # still recorded to the ledger by ``_commit_memory_and_complete_task``
        # and accrues into the hourly-window aggregator; the task reaches
        # ``completed`` despite summarizer_cost > budget_max_per_task.
        task_id = str(uuid.uuid4())
        task_data = await _seed_running_task(integration_pool, task_id=task_id)
        await _seed_checkpoint(integration_pool, task_id)

        # ``pending_memory`` that would blow budget_max_per_task (=1_000) by 5×.
        pending_memory = {
            "title": "Completed",
            "summary": "Done",
            "outcome": "succeeded",
            "content_vec": None,
            "summarizer_model_id": "claude-haiku-4-5",
            "observations_snapshot": ["obs-1"],
            "tags": [],
            "summarizer_tokens_in": 100,
            "summarizer_tokens_out": 50,
            "summarizer_cost_microdollars": 5_000,
            "embedding_tokens": 0,
            "embedding_cost_microdollars": 0,
        }

        events = [
            # The one event the executor sees — a memory_write super-step.
            # The carve-out at graph.py:1700 must skip cost accounting /
            # budget enforcement on this event; the summarizer cost is
            # routed through the commit path instead.
            {
                "memory_write": {
                    "pending_memory": pending_memory,
                    "messages": [],
                }
            },
        ]
        final_state_values = {
            "messages": [_make_ai_msg_with_cost(
                content="Final", input_tokens=10, output_tokens=5,
            )],
            "observations": ["obs-1"],
            "pending_memory": pending_memory,
        }

        executor = GraphExecutor(
            WorkerConfig(worker_id=WORKER_ID, tenant_id=TENANT_ID),
            integration_pool,
        )

        # Patch only two narrow surfaces: the graph builder (avoid needing
        # real LLM credentials) and the checkpointer factory. All DB
        # operations, cost ledger writes, budget checks, and commit
        # transactions run against the real Postgres instance.
        fake_graph = MagicMock()
        fake_graph.compile.return_value = _stub_compiled_graph(
            events=events, final_state_values=final_state_values,
        )

        with patch.object(
            executor, "_build_graph", AsyncMock(return_value=fake_graph)
        ), patch(
            "executor.graph.PostgresDurableCheckpointer",
            return_value=_StubCheckpointer(),
        ):
            cancel_event = asyncio.Event()
            await executor.execute_task(task_data, cancel_event)

        async with integration_pool.acquire() as conn:
            task_row = await conn.fetchrow(
                "SELECT status, pause_reason FROM tasks "
                "WHERE task_id = $1::uuid",
                task_id,
            )
            ledger = await conn.fetch(
                "SELECT cost_microdollars, checkpoint_id "
                "FROM agent_cost_ledger "
                "WHERE task_id = $1::uuid ORDER BY created_at",
                task_id,
            )
            hour_cost = await conn.fetchval(
                "SELECT hour_window_cost_microdollars FROM agent_runtime_state "
                "WHERE tenant_id = $1 AND agent_id = $2",
                TENANT_ID, AGENT_ID,
            )
            mem_row = await conn.fetchrow(
                "SELECT observations, summarizer_model_id "
                "FROM agent_memory_entries WHERE task_id = $1::uuid",
                task_id,
            )

        # Invariant 1: the task is completed, not paused.
        assert task_row["status"] == "completed", (
            "memory_write carve-out failed: task paused despite being on the "
            "budget-exempt super-step."
        )
        assert task_row["pause_reason"] is None

        # Invariant 2: the memory row was written.
        assert mem_row is not None
        assert list(mem_row["observations"]) == ["obs-1"]
        assert mem_row["summarizer_model_id"] == "claude-haiku-4-5"

        # Invariant 3: summarizer cost IS in the ledger. The carve-out
        # exempts it from pause enforcement, NOT from cost accounting.
        total_cost = sum(int(r["cost_microdollars"]) for r in ledger)
        assert total_cost >= 5_000, (
            f"summarizer cost missing from agent_cost_ledger: {list(ledger)}"
        )

        # Invariant 4: hourly-window spend accrued the full summarizer cost.
        # Hourly budget is the non-exempt layer — per design doc #14, only
        # the per-task pause is exempt.
        assert int(hour_cost or 0) >= 5_000, (
            f"hour_window_cost_microdollars missed summarizer accrual: "
            f"got {hour_cost}"
        )


# ---------------------------------------------------------------------------
# Scenario 2: Control — non-memory_write event DOES pause on over-budget.
# ---------------------------------------------------------------------------


class TestNonMemoryWriteEventStillPauses:
    @pytest.mark.asyncio
    async def test_agent_event_over_budget_pauses_task(
        self, integration_pool: asyncpg.Pool
    ) -> None:
        # Covers AC-14 control: the carve-out is a NARROW exception for the
        # ``memory_write`` super-step only. An ``agent`` super-step whose
        # chat-model cost exceeds ``budget_max_per_task`` must still trip
        # ``_check_budget_and_pause`` → task moves to ``paused``.
        task_id = str(uuid.uuid4())
        task_data = await _seed_running_task(integration_pool, task_id=task_id)
        await _seed_checkpoint(integration_pool, task_id)

        # Real provider cost math relies on the per-model rates in the
        # ``models`` table. We bypass that by patching ``_calculate_step_cost``
        # to return a deterministic 5_000-microdollar cost so the test does
        # not couple to model pricing data. Everything under that — the
        # ledger write, the budget check, the pause transition — is real.
        events = [
            {"agent": {"messages": [_make_ai_msg_with_cost(
                content="expensive", input_tokens=1_000, output_tokens=500,
            )]}}
        ]
        final_state_values = {
            "messages": [_make_ai_msg_with_cost(
                content="expensive", input_tokens=1_000, output_tokens=500,
            )],
        }

        executor = GraphExecutor(
            WorkerConfig(worker_id=WORKER_ID, tenant_id=TENANT_ID),
            integration_pool,
        )
        fake_graph = MagicMock()
        fake_graph.compile.return_value = _stub_compiled_graph(
            events=events, final_state_values=final_state_values,
        )

        with patch.object(
            executor, "_build_graph", AsyncMock(return_value=fake_graph)
        ), patch(
            "executor.graph.PostgresDurableCheckpointer",
            return_value=_StubCheckpointer(),
        ), patch.object(
            executor, "_calculate_step_cost",
            new_callable=AsyncMock,
            return_value=(5_000, {
                "input_tokens": 1_000,
                "output_tokens": 500,
                "model": "claude-haiku-4-5",
            }),
        ):
            cancel_event = asyncio.Event()
            await executor.execute_task(task_data, cancel_event)

        async with integration_pool.acquire() as conn:
            task_row = await conn.fetchrow(
                "SELECT status, pause_reason, pause_details "
                "FROM tasks WHERE task_id = $1::uuid",
                task_id,
            )
        # The critical assertion: without the carve-out, BOTH scenarios
        # would pause. With the carve-out, only this one does.
        assert task_row["status"] == "paused", (
            "non-memory_write super-step over budget_max_per_task must still "
            "pause; got status=" + str(task_row["status"])
        )
        assert task_row["pause_reason"] == "budget_per_task"
