"""Integration + unit tests for Phase 2 Track 5 Task 8 — attached-memory
injection into the initial prompt.

Covers two layers:

1. **Repository** — :func:`core.memory_repository.resolve_attached_memories_for_task`
   joins ``task_attached_memories`` with ``agent_memory_entries`` under the
   ``(tenant_id, agent_id)`` predicate and drops rows whose memory id no
   longer resolves (deleted since submission, or cross-scope planted).
2. **Renderer** — :func:`executor.memory_graph.build_attached_memories_preamble`
   shapes resolved entries into the documented prompt-prefix block.

Together they implement the worker's "inject attached memories as a
SystemMessage prefix on first execution, never again" contract — the
follow-up-skip part is covered in the integration check on
``checkpoint_tuple_has_prior_history`` (unit level, no live graph needed).
"""

from __future__ import annotations

import os
import uuid
from types import SimpleNamespace

import asyncpg
import pytest

from core.memory_repository import (
    resolve_attached_memories_for_task,
    upsert_memory_entry,
)
from executor.memory_graph import (
    build_attached_memories_preamble,
    checkpoint_tuple_has_prior_history,
)


DB_DSN = os.getenv(
    "E2E_DB_DSN",
    "postgresql://postgres:postgres@localhost:55433/persistent_agent_runtime_e2e",
)

TENANT_ID = "default"
AGENT_ID = "memory-attach-test-agent"
OTHER_AGENT = "memory-attach-other-agent"


async def _scrub(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM task_attached_memories WHERE task_id IN "
            "(SELECT task_id FROM tasks WHERE tenant_id = $1)",
            TENANT_ID,
        )
        await conn.execute(
            "DELETE FROM agent_memory_entries WHERE tenant_id = $1", TENANT_ID
        )
        # task_events has FK → tasks; scrub before deleting tasks.
        await conn.execute(
            "DELETE FROM task_events WHERE tenant_id = $1 "
            "AND agent_id = ANY($2::text[])",
            TENANT_ID, [AGENT_ID, OTHER_AGENT],
        )
        await conn.execute(
            "DELETE FROM tasks WHERE tenant_id = $1 "
            "AND agent_id = ANY($2::text[])",
            TENANT_ID, [AGENT_ID, OTHER_AGENT],
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
                VALUES ($1, $2, 'Attach Test', '{}'::jsonb, 'active')
                ON CONFLICT (tenant_id, agent_id) DO NOTHING
                """,
                TENANT_ID, agent,
            )
    try:
        yield pool
    finally:
        await _scrub(pool)
        await pool.close()


async def _insert_task(pool: asyncpg.Pool, *, task_id: str) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO tasks (task_id, tenant_id, agent_id,
                               agent_config_snapshot, status, input, version)
            VALUES ($1::uuid, $2, $3, '{}'::jsonb, 'queued', 'x', 1)
            """,
            task_id, TENANT_ID, AGENT_ID,
        )


async def _insert_memory(
    pool: asyncpg.Pool,
    *,
    agent_id: str,
    task_id: str,
    title: str,
    summary: str = "summary text",
    observations: list[str] | None = None,
) -> str:
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await upsert_memory_entry(
                conn,
                {
                    "tenant_id": TENANT_ID,
                    "agent_id": agent_id,
                    "task_id": task_id,
                    "title": title,
                    "summary": summary,
                    "observations": list(observations or []),
                    "outcome": "succeeded",
                    "tags": [],
                    "content_vec": None,
                    "summarizer_model_id": "claude-haiku-4-5",
                },
            )
    return str(row["memory_id"])


async def _attach(
    pool: asyncpg.Pool,
    *,
    task_id: str,
    memory_id: str,
    position: int,
) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO task_attached_memories
                (task_id, memory_id, position)
            VALUES ($1::uuid, $2::uuid, $3)
            """,
            task_id, memory_id, position,
        )


# ---------------------------------------------------------------------------


class TestResolveAttachedMemoriesForTask:
    @pytest.mark.asyncio
    async def test_resolves_multiple_in_position_order(
        self, integration_pool: asyncpg.Pool,
    ) -> None:
        task_id = str(uuid.uuid4())
        await _insert_task(integration_pool, task_id=task_id)
        # Two attached memories — separate origin tasks (memory rows are
        # keyed by their own task_id; the attachment task is different).
        origin_a = str(uuid.uuid4())
        origin_b = str(uuid.uuid4())
        mid_a = await _insert_memory(
            integration_pool, agent_id=AGENT_ID, task_id=origin_a,
            title="Memory A", summary="summary A",
            observations=["a-obs-1", "a-obs-2"],
        )
        mid_b = await _insert_memory(
            integration_pool, agent_id=AGENT_ID, task_id=origin_b,
            title="Memory B", summary="summary B",
            observations=[],
        )
        # Attach in reverse order so we can verify position-ordering.
        await _attach(integration_pool, task_id=task_id, memory_id=mid_b, position=1)
        await _attach(integration_pool, task_id=task_id, memory_id=mid_a, position=0)

        async with integration_pool.acquire() as conn:
            resolved = await resolve_attached_memories_for_task(
                conn, TENANT_ID, AGENT_ID, task_id,
            )
        assert [r["title"] for r in resolved] == ["Memory A", "Memory B"]
        assert resolved[0]["observations"] == ["a-obs-1", "a-obs-2"]
        assert resolved[1]["observations"] == []

    @pytest.mark.asyncio
    async def test_deleted_memory_silently_omitted(
        self, integration_pool: asyncpg.Pool,
    ) -> None:
        task_id = str(uuid.uuid4())
        await _insert_task(integration_pool, task_id=task_id)
        origin = str(uuid.uuid4())
        mid = await _insert_memory(
            integration_pool, agent_id=AGENT_ID, task_id=origin,
            title="Will be deleted",
        )
        await _attach(integration_pool, task_id=task_id, memory_id=mid, position=0)
        # Now delete the memory entry (attachment row persists — no FK cascade).
        async with integration_pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM agent_memory_entries WHERE memory_id = $1::uuid",
                mid,
            )
            # Attachment row still present:
            present = await conn.fetchval(
                "SELECT COUNT(*) FROM task_attached_memories "
                "WHERE task_id = $1::uuid",
                task_id,
            )
        assert present == 1
        async with integration_pool.acquire() as conn:
            resolved = await resolve_attached_memories_for_task(
                conn, TENANT_ID, AGENT_ID, task_id,
            )
        # Silently omitted.
        assert resolved == []

    @pytest.mark.asyncio
    async def test_cross_agent_attachment_is_silently_omitted(
        self, integration_pool: asyncpg.Pool,
    ) -> None:
        task_id = str(uuid.uuid4())
        await _insert_task(integration_pool, task_id=task_id)
        origin = str(uuid.uuid4())
        # Memory belongs to OTHER_AGENT.
        mid = await _insert_memory(
            integration_pool, agent_id=OTHER_AGENT, task_id=origin,
            title="Cross-agent",
        )
        await _attach(integration_pool, task_id=task_id, memory_id=mid, position=0)

        async with integration_pool.acquire() as conn:
            resolved = await resolve_attached_memories_for_task(
                conn, TENANT_ID, AGENT_ID, task_id,
            )
        # LEFT JOIN with (tenant_id, agent_id) predicate drops the row.
        assert resolved == []

    @pytest.mark.asyncio
    async def test_no_attachments_returns_empty_list(
        self, integration_pool: asyncpg.Pool,
    ) -> None:
        task_id = str(uuid.uuid4())
        await _insert_task(integration_pool, task_id=task_id)
        async with integration_pool.acquire() as conn:
            resolved = await resolve_attached_memories_for_task(
                conn, TENANT_ID, AGENT_ID, task_id,
            )
        assert resolved == []


# ---------------------------------------------------------------------------
# Pure-unit tests for the preamble renderer + first-execution predicate.
# ---------------------------------------------------------------------------


class TestBuildAttachedMemoriesPreamble:
    def test_empty_list_returns_none(self) -> None:
        assert build_attached_memories_preamble([]) is None

    def test_single_entry_with_observations(self) -> None:
        out = build_attached_memories_preamble([{
            "position": 0,
            "memory_id": "mid",
            "title": "Past task",
            "summary": "did a thing",
            "observations": ["obs-1", "obs-2"],
        }])
        assert out is not None
        assert "[Attached memory: Past task]" in out
        assert "Observations:" in out
        assert "- obs-1" in out
        assert "- obs-2" in out
        assert "Summary: did a thing" in out

    def test_entry_with_no_observations_uses_none_marker(self) -> None:
        out = build_attached_memories_preamble([{
            "position": 0,
            "memory_id": "mid",
            "title": "Past task",
            "summary": "did a thing",
            "observations": [],
        }])
        assert out is not None
        assert "Observations: (none)" in out

    def test_multiple_entries_separated_by_blank_line(self) -> None:
        out = build_attached_memories_preamble([
            {"position": 0, "memory_id": "a", "title": "A",
             "summary": "sa", "observations": []},
            {"position": 1, "memory_id": "b", "title": "B",
             "summary": "sb", "observations": ["ob"]},
        ])
        assert out is not None
        assert out.count("[Attached memory:") == 2
        assert out.index("[Attached memory: A]") < out.index(
            "[Attached memory: B]"
        )
        # Double newline separation.
        assert "\n\n" in out

    def test_long_title_is_capped(self) -> None:
        long_title = "t" * 500
        out = build_attached_memories_preamble([{
            "position": 0, "memory_id": "m", "title": long_title,
            "summary": "s", "observations": [],
        }])
        assert out is not None
        # Title cap is 200 chars (design-doc consistent).
        rendered_title_line = [
            line for line in out.splitlines()
            if line.startswith("[Attached memory:")
        ][0]
        # Format: "[Attached memory: <title>]" — extract title body.
        inner = rendered_title_line[len("[Attached memory: "): -1]
        assert len(inner) == 200

    def test_long_observation_is_capped(self) -> None:
        long_obs = "o" * 5000
        out = build_attached_memories_preamble([{
            "position": 0, "memory_id": "m", "title": "t",
            "summary": "s", "observations": [long_obs],
        }])
        assert out is not None
        # Observation cap is 2000 chars; prefix "- " adds 2.
        obs_lines = [
            line for line in out.splitlines()
            if line.startswith("- ")
        ]
        assert len(obs_lines) == 1
        assert len(obs_lines[0]) == 2 + 2000


class TestCheckpointTupleHasPriorHistory:
    def test_none_tuple_is_first_run(self) -> None:
        assert checkpoint_tuple_has_prior_history(None) is False

    def test_tuple_without_checkpoint_attr_is_first_run(self) -> None:
        obj = SimpleNamespace()
        assert checkpoint_tuple_has_prior_history(obj) is False

    def test_tuple_with_empty_messages_is_first_run(self) -> None:
        # LangGraph durability modes can persist an empty state before the
        # first super-step in edge cases — treat that as first-run.
        obj = SimpleNamespace(
            checkpoint={"channel_values": {"messages": []}}
        )
        assert checkpoint_tuple_has_prior_history(obj) is False

    def test_tuple_without_messages_key_is_first_run(self) -> None:
        obj = SimpleNamespace(
            checkpoint={"channel_values": {"observations": ["x"]}}
        )
        assert checkpoint_tuple_has_prior_history(obj) is False

    def test_tuple_with_messages_is_follow_up(self) -> None:
        obj = SimpleNamespace(
            checkpoint={"channel_values": {
                "messages": ["existing-message"],
            }}
        )
        assert checkpoint_tuple_has_prior_history(obj) is True
