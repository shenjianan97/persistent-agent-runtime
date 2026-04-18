"""asyncpg helpers for the ``checkpoints`` table.

Consolidates the read/write patterns that `executor/graph.py` previously
inlined across the streaming step-cost path, the post-astream memory-write
commit, the sandbox-cleanup path, and `record_step_cost`.

The caller owns the asyncpg connection and the transaction boundary — every
function here takes ``conn`` as the first argument. That lets the lease-
validated ``UPDATE tasks ...`` and the checkpoint write live in a single
transaction, preserving the invariant that a revoked lease rolls back every
accompanying write.

Two distinct "latest checkpoint" lookups live here. They are intentionally
kept separate:

* :func:`fetch_latest_checkpoint_id` — no namespace filter. Used from the
  streaming step-cost path where LangGraph may interleave subgraph
  checkpoints (non-empty ``checkpoint_ns``) with main-graph ones.
* :func:`fetch_latest_terminal_checkpoint_id` — filters to ``checkpoint_ns
  = ''``. Used from the terminal commit path (memory-write, sandbox-cleanup,
  dead-letter) where cost must attribute to the main-graph's terminal
  checkpoint so the API's per-step timeline surfaces the spend.
"""

from __future__ import annotations

import json
from typing import Any

import asyncpg


_FETCH_LATEST_CHECKPOINT_ID_SQL = """
SELECT checkpoint_id FROM checkpoints
WHERE task_id = $1::uuid
ORDER BY created_at DESC LIMIT 1
"""

_FETCH_LATEST_TERMINAL_CHECKPOINT_ID_SQL = """
SELECT checkpoint_id FROM checkpoints
WHERE task_id = $1::uuid AND checkpoint_ns = ''
ORDER BY created_at DESC LIMIT 1
"""

_SET_COST_AND_METADATA_SQL = """
UPDATE checkpoints
SET cost_microdollars = $1,
    execution_metadata = $4::jsonb
WHERE checkpoint_id = $2
  AND task_id = $3::uuid
"""

_ADD_COST_AND_PRESERVE_METADATA_SQL = """
UPDATE checkpoints
SET cost_microdollars = cost_microdollars + $1,
    execution_metadata = COALESCE(execution_metadata, $2::jsonb)
WHERE checkpoint_id = $3
  AND task_id = $4::uuid
"""

_SET_EXECUTION_METADATA_SQL = """
UPDATE checkpoints
SET execution_metadata = $1::jsonb
WHERE checkpoint_id = $2
  AND task_id = $3::uuid
"""

# Embedded subquery resolves the terminal-checkpoint target atomically with
# the cost UPDATE so a concurrent checkpoint insert can't shift the target
# row between the SELECT and the UPDATE.
_ADD_COST_TO_LATEST_TERMINAL_CHECKPOINT_SQL = """
UPDATE checkpoints
SET cost_microdollars = cost_microdollars + $1
WHERE checkpoint_id = (
    SELECT checkpoint_id FROM checkpoints
    WHERE task_id = $2::uuid AND checkpoint_ns = ''
    ORDER BY created_at DESC LIMIT 1
)
"""


def _encode_metadata(metadata: dict[str, Any] | None) -> str | None:
    return json.dumps(metadata) if metadata is not None else None


async def fetch_latest_checkpoint_id(
    conn: asyncpg.Connection,
    task_id: str,
) -> str | None:
    """Return the most recent checkpoint id for the task across all namespaces."""
    return await conn.fetchval(_FETCH_LATEST_CHECKPOINT_ID_SQL, task_id)


async def fetch_latest_terminal_checkpoint_id(
    conn: asyncpg.Connection,
    task_id: str,
) -> str | None:
    """Return the most recent main-graph (``checkpoint_ns=''``) checkpoint id."""
    return await conn.fetchval(
        _FETCH_LATEST_TERMINAL_CHECKPOINT_ID_SQL, task_id
    )


async def set_cost_and_metadata(
    conn: asyncpg.Connection,
    *,
    checkpoint_id: str,
    task_id: str,
    cost_microdollars: int,
    execution_metadata: dict[str, Any] | None,
) -> None:
    """Overwrite ``cost_microdollars`` and ``execution_metadata`` for a checkpoint.

    Used by :func:`record_step_cost` which owns first-write attribution for a
    freshly-emitted checkpoint — nothing has accrued onto it yet, so overwrite
    is safe.
    """
    await conn.execute(
        _SET_COST_AND_METADATA_SQL,
        cost_microdollars,
        checkpoint_id,
        task_id,
        _encode_metadata(execution_metadata),
    )


async def add_cost_and_preserve_metadata(
    conn: asyncpg.Connection,
    *,
    checkpoint_id: str,
    task_id: str,
    delta_microdollars: int,
    execution_metadata: dict[str, Any] | None,
) -> None:
    """Additively update ``cost_microdollars`` and preserve prior metadata.

    Memory-write commit uses this because the sandbox-cleanup path may have
    already added runtime spend onto the same checkpoint; an overwrite would
    silently drop that spend from the timeline. ``execution_metadata`` is set
    only when NULL (``COALESCE``) so an earlier writer's payload isn't
    clobbered either.
    """
    await conn.execute(
        _ADD_COST_AND_PRESERVE_METADATA_SQL,
        delta_microdollars,
        _encode_metadata(execution_metadata),
        checkpoint_id,
        task_id,
    )


async def set_execution_metadata(
    conn: asyncpg.Connection,
    *,
    checkpoint_id: str,
    task_id: str,
    execution_metadata: dict[str, Any] | None,
) -> None:
    """Set ``execution_metadata`` without touching ``cost_microdollars``.

    Used when the step cost is zero (unknown model / rounding) but we still
    want token-usage metadata on the step timeline.
    """
    await conn.execute(
        _SET_EXECUTION_METADATA_SQL,
        _encode_metadata(execution_metadata),
        checkpoint_id,
        task_id,
    )


async def add_cost_to_latest_terminal_checkpoint(
    conn: asyncpg.Connection,
    *,
    task_id: str,
    delta_microdollars: int,
) -> None:
    """Mirror a cost delta onto whichever main-graph checkpoint is currently terminal.

    End-of-task sandbox spend uses this — it doesn't know the checkpoint id
    ahead of time and the terminal checkpoint may have been rewritten since
    the caller last observed it.
    """
    await conn.execute(
        _ADD_COST_TO_LATEST_TERMINAL_CHECKPOINT_SQL,
        delta_microdollars,
        task_id,
    )
