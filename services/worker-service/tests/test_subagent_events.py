"""Integration tests for sub-agent / Supervisor task_events (migration 0025 + S9).

Covers the worker half of Task S9:

1. Migration ``0025`` admits the four new ``event_type`` values
   (``subagent_started`` / ``subagent_finding`` / ``subagent_failed`` /
   ``supervisor_iteration``), still rejects a bogus type, AND still rejects
   ``subagent_heartbeat`` (deliberately a Langfuse span event, NOT a task_events
   row — §A11-E4).
2. Each ``emit_*`` helper, driven through the REAL sink
   (:func:`core.subagent_events.build_task_event_sink`), writes one row with the
   correct ``event_type`` and a ``details`` JSONB carrying the documented keys
   (incl. ``iteration`` int + ``subtask`` string where applicable;
   ``prompt_preview`` truncated to the cap).
3. The real sink drops the ``subagent.heartbeat`` span event (the same injected
   ``emit`` carries it) rather than attempting an INSERT the CHECK would reject.

DB-only — no TCP port bind, no server subprocess (worktree-concurrency-safe).
Run via the isolated harness:
    make e2e-test PYTEST_ARGS='-k subagent_event'
"""

from __future__ import annotations

import json
import os
import uuid

import asyncpg
import pytest

from core.subagent_events import (
    PROMPT_PREVIEW_MAX_CHARS,
    SUBAGENT_COMPLETED_EVENT,
    SUBAGENT_FAILED_EVENT,
    SUBAGENT_FINDING_EVENT,
    SUBAGENT_STARTED_EVENT,
    SUPERVISOR_ITERATION_EVENT,
    build_task_event_sink,
    emit_subagent_completed,
    emit_subagent_failed,
    emit_subagent_finding,
    emit_subagent_started,
    emit_supervisor_iteration,
)

DB_DSN = os.getenv(
    "E2E_DB_DSN",
    "postgresql://postgres:postgres@localhost:55433/persistent_agent_runtime_e2e",
)

TENANT_ID = "default"
AGENT_ID = "subagent-events-test-agent"

NEW_EVENT_TYPES = [
    SUBAGENT_STARTED_EVENT,
    SUBAGENT_FINDING_EVENT,
    SUBAGENT_FAILED_EVENT,
    SUPERVISOR_ITERATION_EVENT,
    # Migration 0026 — terminal success marker (counterpart to subagent_failed).
    SUBAGENT_COMPLETED_EVENT,
]


@pytest.fixture
async def integration_pool():
    try:
        pool = await asyncpg.create_pool(DB_DSN, min_size=1, max_size=3)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"PostgreSQL test database is not available: {exc}")

    async with pool.acquire() as conn:
        # task_events.task_id FKs tasks(task_id); clean leftover rows first.
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
            VALUES ($1, $2, 'Sub-Agent Events Test Agent', '{}'::jsonb, 'active')
            """,
            TENANT_ID, AGENT_ID,
        )

    try:
        yield pool
    finally:
        async with pool.acquire() as conn:
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


async def _new_task(conn: asyncpg.Connection) -> str:
    """Insert a minimal running task row and return its task_id (str)."""
    task_id = await conn.fetchval(
        """
        INSERT INTO tasks (
            tenant_id, agent_id, agent_config_snapshot,
            status, input, max_retries, retry_count
        )
        VALUES ($1, $2, '{}'::jsonb, 'running', 'go', 3, 0)
        RETURNING task_id
        """,
        TENANT_ID, AGENT_ID,
    )
    return str(task_id)


async def _fetch_one_event(conn: asyncpg.Connection, task_id: str) -> dict:
    """Return the single task_events row for ``task_id`` (event_type + details)."""
    rows = await conn.fetch(
        "SELECT event_type, status_before, status_after, details "
        "FROM task_events WHERE task_id = $1::uuid",
        task_id,
    )
    assert len(rows) == 1, f"expected exactly one event row, got {len(rows)}"
    row = rows[0]
    details = row["details"]
    if isinstance(details, str):  # asyncpg returns JSONB as text by default
        details = json.loads(details)
    return {
        "event_type": row["event_type"],
        "status_before": row["status_before"],
        "status_after": row["status_after"],
        "details": details,
    }


# --------------------------------------------------------------------------- #
# 1. Migration 0025 — CHECK acceptance / rejection
# --------------------------------------------------------------------------- #
class TestMigration0025CheckConstraint:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("event_type", NEW_EVENT_TYPES)
    async def test_each_new_event_type_is_admitted(
        self, integration_pool: asyncpg.Pool, event_type: str
    ) -> None:
        async with integration_pool.acquire() as conn:
            task_id = await _new_task(conn)
            await conn.execute(
                "INSERT INTO task_events (tenant_id, task_id, agent_id, event_type, "
                "details) VALUES ($1, $2::uuid, $3, $4, '{}'::jsonb)",
                TENANT_ID, task_id, AGENT_ID, event_type,
            )
        # No CHECK violation raised == admitted.

    @pytest.mark.asyncio
    async def test_bogus_event_type_is_rejected(
        self, integration_pool: asyncpg.Pool
    ) -> None:
        async with integration_pool.acquire() as conn:
            task_id = await _new_task(conn)
            with pytest.raises(asyncpg.CheckViolationError):
                await conn.execute(
                    "INSERT INTO task_events (tenant_id, task_id, agent_id, "
                    "event_type, details) VALUES ($1, $2::uuid, $3, $4, '{}'::jsonb)",
                    TENANT_ID, task_id, AGENT_ID, "not_a_real_event_type",
                )

    @pytest.mark.asyncio
    async def test_subagent_heartbeat_is_rejected(
        self, integration_pool: asyncpg.Pool
    ) -> None:
        """subagent_heartbeat is a Langfuse span event — NOT an admitted type."""
        async with integration_pool.acquire() as conn:
            task_id = await _new_task(conn)
            with pytest.raises(asyncpg.CheckViolationError):
                await conn.execute(
                    "INSERT INTO task_events (tenant_id, task_id, agent_id, "
                    "event_type, details) VALUES ($1, $2::uuid, $3, $4, '{}'::jsonb)",
                    TENANT_ID, task_id, AGENT_ID, "subagent_heartbeat",
                )

    @pytest.mark.asyncio
    async def test_constraint_clause_contains_all_subagent_markers(
        self, integration_pool: asyncpg.Pool
    ) -> None:
        async with integration_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT check_clause FROM information_schema.check_constraints "
                "WHERE constraint_name = 'task_events_event_type_check'",
            )
        assert row is not None, "task_events_event_type_check must exist"
        clause = row["check_clause"]
        for event_type in NEW_EVENT_TYPES:
            assert event_type in clause, f"{event_type} missing from {clause}"
        # subagent_completed (migration 0026) is the terminal success marker.
        assert SUBAGENT_COMPLETED_EVENT in clause
        assert "subagent_heartbeat" not in clause
        # Additive — a pre-existing value must survive.
        assert "memory_written" in clause


# --------------------------------------------------------------------------- #
# 2. emit_* helpers through the REAL sink
# --------------------------------------------------------------------------- #
class TestEmitHelpersThroughRealSink:
    @pytest.mark.asyncio
    async def test_emit_supervisor_iteration(
        self, integration_pool: asyncpg.Pool
    ) -> None:
        async with integration_pool.acquire() as conn:
            task_id = await _new_task(conn)
        sink = build_task_event_sink(
            integration_pool, task_id=task_id, tenant_id=TENANT_ID, agent_id=AGENT_ID
        )
        await emit_supervisor_iteration(
            sink, iteration=2, subtasks_emitted=3, decision="continue", reason="more"
        )
        async with integration_pool.acquire() as conn:
            ev = await _fetch_one_event(conn, task_id)
        assert ev["event_type"] == SUPERVISOR_ITERATION_EVENT
        assert ev["status_before"] is None and ev["status_after"] is None
        assert ev["details"] == {
            "iteration": 2,
            "subtasks_emitted": 3,
            "decision": "continue",
            "reason": "more",
        }

    @pytest.mark.asyncio
    async def test_emit_subagent_started_truncates_prompt_preview(
        self, integration_pool: asyncpg.Pool
    ) -> None:
        async with integration_pool.acquire() as conn:
            task_id = await _new_task(conn)
        sink = build_task_event_sink(
            integration_pool, task_id=task_id, tenant_id=TENANT_ID, agent_id=AGENT_ID
        )
        long_prompt = "x" * (PROMPT_PREVIEW_MAX_CHARS + 500)
        await emit_subagent_started(
            sink,
            iteration=1,
            subtask="1.0",
            prompt_preview=long_prompt,
            tool_allowlist=["web_search", "read_url"],
            depth=1,
        )
        async with integration_pool.acquire() as conn:
            ev = await _fetch_one_event(conn, task_id)
        assert ev["event_type"] == SUBAGENT_STARTED_EVENT
        details = ev["details"]
        assert details["iteration"] == 1
        assert details["subtask"] == "1.0"
        assert details["depth"] == 1
        assert details["tool_allowlist"] == ["web_search", "read_url"]
        assert len(details["prompt_preview"]) == PROMPT_PREVIEW_MAX_CHARS
        assert details["prompt_preview"] == "x" * PROMPT_PREVIEW_MAX_CHARS

    @pytest.mark.asyncio
    async def test_emit_subagent_finding(
        self, integration_pool: asyncpg.Pool
    ) -> None:
        async with integration_pool.acquire() as conn:
            task_id = await _new_task(conn)
        sink = build_task_event_sink(
            integration_pool, task_id=task_id, tenant_id=TENANT_ID, agent_id=AGENT_ID
        )
        await emit_subagent_finding(
            sink,
            iteration=1,
            subtask="1.2",
            finding_id="f-abc123",
            source_url="https://example.com/a",
        )
        async with integration_pool.acquire() as conn:
            ev = await _fetch_one_event(conn, task_id)
        assert ev["event_type"] == SUBAGENT_FINDING_EVENT
        assert ev["details"] == {
            "iteration": 1,
            "subtask": "1.2",
            "finding_id": "f-abc123",
            "source_url": "https://example.com/a",
        }
        # claim / supporting_quote MUST NOT ride the row (§A7 — Langfuse span).
        assert "claim" not in ev["details"]
        assert "supporting_quote" not in ev["details"]

    @pytest.mark.asyncio
    async def test_emit_subagent_failed(
        self, integration_pool: asyncpg.Pool
    ) -> None:
        async with integration_pool.acquire() as conn:
            task_id = await _new_task(conn)
        sink = build_task_event_sink(
            integration_pool, task_id=task_id, tenant_id=TENANT_ID, agent_id=AGENT_ID
        )
        await emit_subagent_failed(
            sink, iteration=3, subtask="3.1", reason="timeout"
        )
        async with integration_pool.acquire() as conn:
            ev = await _fetch_one_event(conn, task_id)
        assert ev["event_type"] == SUBAGENT_FAILED_EVENT
        assert ev["details"] == {
            "iteration": 3,
            "subtask": "3.1",
            "reason": "timeout",
        }

    @pytest.mark.asyncio
    async def test_emit_subagent_completed(
        self, integration_pool: asyncpg.Pool
    ) -> None:
        async with integration_pool.acquire() as conn:
            task_id = await _new_task(conn)
        sink = build_task_event_sink(
            integration_pool, task_id=task_id, tenant_id=TENANT_ID, agent_id=AGENT_ID
        )
        await emit_subagent_completed(sink, iteration=2, subtask="2.0")
        async with integration_pool.acquire() as conn:
            ev = await _fetch_one_event(conn, task_id)
        assert ev["event_type"] == SUBAGENT_COMPLETED_EVENT
        assert ev["status_before"] is None and ev["status_after"] is None
        # Minimal lifecycle payload — NO finding_count or other result (§A7).
        assert ev["details"] == {"iteration": 2, "subtask": "2.0"}
        assert "finding_count" not in ev["details"]

    @pytest.mark.asyncio
    async def test_emit_subagent_failed_carries_truncated_detail(
        self, integration_pool: asyncpg.Pool
    ) -> None:
        # Regression for task 0729e3a3 sub-agent 1.3: the marker said only
        # "error" while the actual cause (a Bedrock read-timeout) lived in
        # the worker log. The payload now carries the cause, capped.
        from core.subagent_events import FAILURE_DETAIL_MAX_CHARS

        async with integration_pool.acquire() as conn:
            task_id = await _new_task(conn)
        sink = build_task_event_sink(
            integration_pool, task_id=task_id, tenant_id=TENANT_ID, agent_id=AGENT_ID
        )
        long_detail = "ReadTimeoutError: Read timeout on endpoint URL " + "x" * 400
        await emit_subagent_failed(
            sink, iteration=1, subtask="1.3", reason="error", detail=long_detail
        )
        async with integration_pool.acquire() as conn:
            ev = await _fetch_one_event(conn, task_id)
        assert ev["details"]["reason"] == "error"
        assert ev["details"]["detail"] == long_detail[:FAILURE_DETAIL_MAX_CHARS]
        assert len(ev["details"]["detail"]) == FAILURE_DETAIL_MAX_CHARS


# --------------------------------------------------------------------------- #
# 3. The real sink drops the heartbeat span event (never an INSERT)
# --------------------------------------------------------------------------- #
class TestRealSinkDropsHeartbeat:
    @pytest.mark.asyncio
    async def test_heartbeat_event_is_not_written(
        self, integration_pool: asyncpg.Pool
    ) -> None:
        async with integration_pool.acquire() as conn:
            task_id = await _new_task(conn)
        sink = build_task_event_sink(
            integration_pool, task_id=task_id, tenant_id=TENANT_ID, agent_id=AGENT_ID
        )
        # The same injected sink carries the Langfuse span heartbeat. It must be
        # dropped (no CheckViolationError, no row written) rather than INSERTed.
        await sink("subagent.heartbeat", {"turn": 0, "tokens": 0})
        async with integration_pool.acquire() as conn:
            count = await conn.fetchval(
                "SELECT count(*) FROM task_events WHERE task_id = $1::uuid", task_id
            )
        assert count == 0, "heartbeat span event must not be written to task_events"
