"""Integration tests for dead_letter_reason CHECK constraints (migration 0015).

Verifies that:
1. The tasks.tasks_dead_letter_reason_check constraint includes
   'context_exceeded_irrecoverable'.
2. Inserting a task row with dead_letter_reason='context_exceeded_irrecoverable'
   succeeds (constraint accepts the new value).
3. Inserting a task row with an unknown dead_letter_reason is rejected by the
   constraint.
4. There is no separate CHECK constraint on task_events.dead_letter_reason
   (the column does not exist on that table — confirmed by schema inspection).

Run against the isolated test DB on port 55433:
    services/worker-service/.venv/bin/python -m pytest -xvs \\
        services/worker-service/tests/test_dead_letter_check_constraints_integration.py

Track 7, Task 10 — Context Window Management dead-letter reason expansion.
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
AGENT_ID = "dead-letter-check-constraint-test-agent"

# The full set of allowed dead_letter_reason values after migration 0015.
# Keep this in sync with ValidationConstants.ALLOWED_DEAD_LETTER_REASONS.
EXPECTED_ALLOWED_REASONS = {
    "cancelled_by_user",
    "retries_exhausted",
    "task_timeout",
    "non_retryable_error",
    "max_steps_exceeded",
    "human_input_timeout",
    "rejected_by_user",
    "sandbox_lost",
    "sandbox_provision_failed",
    "context_exceeded_irrecoverable",
}


@pytest.fixture
async def integration_pool():
    try:
        pool = await asyncpg.create_pool(DB_DSN, min_size=1, max_size=2)
    except Exception as exc:
        pytest.skip(f"PostgreSQL test database is not available: {exc}")

    async with pool.acquire() as conn:
        # Clean up any leftover rows from previous runs.
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
            VALUES ($1, $2, 'Dead-Letter CHECK Constraint Test Agent', '{}'::jsonb, 'active')
            """,
            TENANT_ID, AGENT_ID,
        )

    try:
        yield pool
    finally:
        async with pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM tasks WHERE tenant_id = $1 AND agent_id = $2",
                TENANT_ID, AGENT_ID,
            )
            await conn.execute(
                "DELETE FROM agents WHERE tenant_id = $1 AND agent_id = $2",
                TENANT_ID, AGENT_ID,
            )
        await pool.close()


async def _insert_dead_letter_task(
    conn: asyncpg.Connection,
    *,
    dead_letter_reason: str,
) -> uuid.UUID:
    """Insert a minimal task row in dead_letter status with the given reason."""
    task_id = await conn.fetchval(
        """
        INSERT INTO tasks (
            tenant_id, agent_id, agent_config_snapshot,
            status, dead_letter_reason, dead_lettered_at,
            input, max_retries, retry_count
        )
        VALUES (
            $1, $2, '{}'::jsonb,
            'dead_letter', $3, NOW(),
            'test input', 3, 3
        )
        RETURNING task_id
        """,
        TENANT_ID, AGENT_ID, dead_letter_reason,
    )
    return task_id


class TestTasksDeadLetterReasonConstraint:
    """tasks.tasks_dead_letter_reason_check covers context_exceeded_irrecoverable."""

    @pytest.mark.asyncio
    async def test_context_exceeded_irrecoverable_is_accepted(
        self, integration_pool: asyncpg.Pool
    ) -> None:
        """The new reason must be accepted by the CHECK constraint after migration 0015."""
        async with integration_pool.acquire() as conn:
            task_id = await _insert_dead_letter_task(
                conn, dead_letter_reason="context_exceeded_irrecoverable"
            )
        assert task_id is not None

    @pytest.mark.asyncio
    async def test_unknown_dead_letter_reason_is_rejected(
        self, integration_pool: asyncpg.Pool
    ) -> None:
        """A reason not in the allowed set must be rejected."""
        async with integration_pool.acquire() as conn:
            with pytest.raises(asyncpg.CheckViolationError):
                await _insert_dead_letter_task(
                    conn, dead_letter_reason="not_a_real_reason"
                )

    @pytest.mark.asyncio
    async def test_null_dead_letter_reason_is_accepted(
        self, integration_pool: asyncpg.Pool
    ) -> None:
        """NULL is always valid — running tasks have no dead_letter_reason."""
        async with integration_pool.acquire() as conn:
            task_id = await conn.fetchval(
                """
                INSERT INTO tasks (
                    tenant_id, agent_id, agent_config_snapshot,
                    status, input, max_retries, retry_count
                )
                VALUES ($1, $2, '{}'::jsonb, 'running', 'test input', 3, 0)
                RETURNING task_id
                """,
                TENANT_ID, AGENT_ID,
            )
        assert task_id is not None

    @pytest.mark.asyncio
    async def test_constraint_clause_contains_context_exceeded_irrecoverable(
        self, integration_pool: asyncpg.Pool
    ) -> None:
        """Schema inspection: the constraint definition includes the new value.

        This is the mechanical gate from the task spec — even without exercising
        the write path, it confirms migration 0015 applied correctly.
        """
        async with integration_pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT check_clause
                FROM information_schema.check_constraints
                WHERE constraint_name = 'tasks_dead_letter_reason_check'
                """,
            )
        assert row is not None, "tasks_dead_letter_reason_check constraint must exist"
        assert "context_exceeded_irrecoverable" in row["check_clause"], (
            f"Constraint clause must include 'context_exceeded_irrecoverable'. "
            f"Got: {row['check_clause']}"
        )


class TestAllAllowedReasonsAreAccepted:
    """Every value in the allowed-set must be accepted by the DB constraint."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("reason", sorted(EXPECTED_ALLOWED_REASONS))
    async def test_each_allowed_reason_is_accepted(
        self, integration_pool: asyncpg.Pool, reason: str
    ) -> None:
        async with integration_pool.acquire() as conn:
            task_id = await _insert_dead_letter_task(conn, dead_letter_reason=reason)
        assert task_id is not None, f"Reason '{reason}' should be accepted"


class TestTaskEventsHasNoDeadLetterReasonConstraint:
    """task_events does not have a dead_letter_reason column.

    Confirms that there is no separate constraint to extend (the task spec
    required an explicit check for this). The dead_letter_reason information
    lives in tasks.dead_letter_reason and in task_events.details JSONB.
    """

    @pytest.mark.asyncio
    async def test_task_events_has_no_dead_letter_reason_column(
        self, integration_pool: asyncpg.Pool
    ) -> None:
        async with integration_pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_name = 'task_events'
                  AND column_name = 'dead_letter_reason'
                """,
            )
        assert row is None, (
            "task_events must NOT have a dead_letter_reason column — "
            "that information lives in tasks.dead_letter_reason and "
            "task_events.details JSONB."
        )
