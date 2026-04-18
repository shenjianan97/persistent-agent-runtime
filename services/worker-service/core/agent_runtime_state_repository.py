"""asyncpg helpers for the ``agent_runtime_state`` table.

Collapses the two UPSERT shapes that `executor/graph.py` and `core/reaper.py`
previously inlined at ~11 call sites. Both UPSERTs share the same insert
sentinel row (``running_task_count=0``, ``hour_window_cost_microdollars=0``,
``scheduler_cursor='1970-01-01T00:00:00Z'``) so a first-write on a fresh
``(tenant_id, agent_id)`` materializes a zeroed baseline before the conflict
branch applies the intended delta.

The caller owns the asyncpg connection and the transaction boundary — every
function here takes ``conn`` as its first argument and expects to run inside
an active transaction, mirroring :mod:`core.memory_repository`. That keeps
the lease-validated ``UPDATE tasks ...`` and the runtime-state write atomic
under a single lease pin.
"""

from __future__ import annotations

import asyncpg


_INCREMENT_HOUR_WINDOW_COST_SQL = """
INSERT INTO agent_runtime_state
    (tenant_id, agent_id, running_task_count,
     hour_window_cost_microdollars, scheduler_cursor, updated_at)
VALUES ($1, $2, 0, $3, '1970-01-01T00:00:00Z', NOW())
ON CONFLICT (tenant_id, agent_id) DO UPDATE
SET hour_window_cost_microdollars =
        agent_runtime_state.hour_window_cost_microdollars + $3,
    updated_at = NOW()
"""

_DECREMENT_RUNNING_COUNT_SQL = """
INSERT INTO agent_runtime_state
    (tenant_id, agent_id, running_task_count,
     hour_window_cost_microdollars, scheduler_cursor, updated_at)
VALUES ($1, $2, 0, 0, '1970-01-01T00:00:00Z', NOW())
ON CONFLICT (tenant_id, agent_id) DO UPDATE
SET running_task_count =
        GREATEST(agent_runtime_state.running_task_count - 1, 0),
    updated_at = NOW()
"""


async def increment_hour_window_cost(
    conn: asyncpg.Connection,
    tenant_id: str,
    agent_id: str,
    delta_microdollars: int,
) -> None:
    """Add ``delta_microdollars`` to the rolling hourly-cost window.

    Memory-write cost is exempt from the per-task pause check but still
    accrues to the rolling window — callers must treat that invariant
    separately; this helper is purely additive.
    """
    await conn.execute(
        _INCREMENT_HOUR_WINDOW_COST_SQL,
        tenant_id,
        agent_id,
        delta_microdollars,
    )


async def decrement_running_count(
    conn: asyncpg.Connection,
    tenant_id: str,
    agent_id: str,
) -> None:
    """Decrement ``running_task_count`` on a terminal task transition.

    Floored at zero via ``GREATEST(..., 0)`` so reconciliation paths can
    run idempotently without underflowing the counter.
    """
    await conn.execute(
        _DECREMENT_RUNNING_COUNT_SQL,
        tenant_id,
        agent_id,
    )
