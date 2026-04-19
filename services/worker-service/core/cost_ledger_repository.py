"""asyncpg helpers for the ``agent_cost_ledger`` table.

Collapses the INSERT shape that appeared at 7 call sites in
`executor/graph.py` — step-cost, memory summarizer, memory embedding,
sandbox-spend (pause / HITL / terminal), and dead-letter embedding — plus
the two aggregate reads used by the budget-window check.

The caller owns the asyncpg connection and the transaction boundary. Ledger
rows always live in the same transaction as the lease-validated ``UPDATE
tasks ...`` that produced them; passing ``conn`` in from the caller keeps
that invariant explicit.

Migration 0016 extended the table with ``operation``, ``model_id``,
``tokens_in``, ``tokens_out``, and ``summarized_through_turn_index_after``
columns for Track 7 compaction attribution. The extended :func:`insert_cost_row`
signature keeps all new columns as keyword args with defaults so existing callers
are unaffected.
"""

from __future__ import annotations

from datetime import datetime

import asyncpg


# ``checkpoint_id`` is TEXT so callers can pass either the UUID-shaped id of a
# real checkpoint row or the literal ``'sandbox'`` used for sandbox-runtime
# spend, which is attributed per-task but not to a specific step.
#
# The INSERT uses ON CONFLICT DO NOTHING against the partial unique index
# ``idx_agent_cost_ledger_tier3_idempotency`` (Track 7 / migration 0016).
# For non-compaction operations (model_token_spend, sandbox_runtime) no
# conflict will ever fire because the partial index is scoped to
# ``operation = 'compaction.tier3'``.
_INSERT_COST_ROW_SQL = """
INSERT INTO agent_cost_ledger
    (tenant_id, agent_id, task_id, checkpoint_id, cost_microdollars,
     operation, model_id, tokens_in, tokens_out,
     summarized_through_turn_index_after)
VALUES ($1, $2, $3::uuid, $4, $5, $6, $7, $8, $9, $10)
ON CONFLICT DO NOTHING
"""

_SUM_TASK_COST_SQL = """
SELECT COALESCE(SUM(cost_microdollars), 0)
FROM agent_cost_ledger
WHERE task_id = $1::uuid
"""

_SUM_HOURLY_COST_FOR_AGENT_SQL = """
SELECT COALESCE(SUM(cost_microdollars), 0)
FROM agent_cost_ledger
WHERE tenant_id = $1 AND agent_id = $2
  AND created_at > NOW() - INTERVAL '60 minutes'
"""

_MIN_CREATED_AT_IN_HOUR_WINDOW_SQL = """
SELECT MIN(created_at) FROM agent_cost_ledger
WHERE tenant_id = $1 AND agent_id = $2
  AND created_at > NOW() - INTERVAL '60 minutes'
"""


async def insert_cost_row(
    conn: asyncpg.Connection,
    *,
    tenant_id: str,
    agent_id: str,
    task_id: str,
    checkpoint_id: str,
    cost_microdollars: int,
    # Track 7 / migration 0016 extended columns.  Default values keep
    # all pre-Track-7 callers (model_token_spend, sandbox_runtime,
    # memory_write) working without source changes.
    operation: str = "model_token_spend",
    model_id: str | None = None,
    tokens_in: int = 0,
    tokens_out: int = 0,
    summarized_through_turn_index_after: int | None = None,
) -> None:
    """Append one attribution row to the ledger.

    ``checkpoint_id`` is the UUID-shaped id of a real checkpoint row for
    model-token spend, or the literal ``'sandbox'`` for per-task sandbox
    runtime spend. The ledger is append-only; aggregation happens at read
    time via :func:`sum_task_cost` / :func:`sum_hourly_cost_for_agent`.

    For ``operation='compaction.tier3'`` rows the INSERT uses
    ``ON CONFLICT DO NOTHING`` against the partial unique index on
    ``(tenant_id, task_id, checkpoint_id, operation, summarized_through_turn_index_after)``
    so that a crash-after-insert-before-state-commit replay is swallowed
    rather than double-charging.  The ``ON CONFLICT DO NOTHING`` is
    unconditional but the partial index ensures it never fires for
    other operation types, which rely on application-level dedup.
    """
    await conn.execute(
        _INSERT_COST_ROW_SQL,
        tenant_id,
        agent_id,
        task_id,
        checkpoint_id,
        cost_microdollars,
        operation,
        model_id,
        tokens_in,
        tokens_out,
        summarized_through_turn_index_after,
    )


async def sum_task_cost(
    conn: asyncpg.Connection,
    task_id: str,
) -> int:
    """Return the cumulative ledger spend for a task in microdollars."""
    value = await conn.fetchval(_SUM_TASK_COST_SQL, task_id)
    return int(value or 0)


async def sum_hourly_cost_for_agent(
    conn: asyncpg.Connection,
    tenant_id: str,
    agent_id: str,
) -> int:
    """Return the rolling 60-minute-window spend for an agent in microdollars.

    Independent of ``agent_runtime_state.hour_window_cost_microdollars`` —
    the runtime-state value is the eventually-consistent accumulator written
    via UPSERT, whereas this is the authoritative ledger aggregate. The two
    diverge briefly between ledger INSERT and runtime-state UPSERT, which is
    why the budget-pause path reads the ledger, not the accumulator.
    """
    value = await conn.fetchval(
        _SUM_HOURLY_COST_FOR_AGENT_SQL, tenant_id, agent_id
    )
    return int(value or 0)


async def min_created_at_in_hour_window(
    conn: asyncpg.Connection,
    tenant_id: str,
    agent_id: str,
) -> datetime | None:
    """Return the oldest ledger timestamp within the 60-minute window.

    Budget-pause resume-time calculation uses this: the earliest entry
    inside the window dictates when that entry ages out, which is the
    earliest point the agent could be un-paused.
    """
    return await conn.fetchval(
        _MIN_CREATED_AT_IN_HOUR_WINDOW_SQL, tenant_id, agent_id
    )
