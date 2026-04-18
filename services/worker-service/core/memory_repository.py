"""Phase 2 Track 5 — asyncpg helpers for the worker memory write path.

This module owns the DB-shaped part of the Task 6 write path. It is small,
stateless, and intentionally free of LangGraph awareness so the caller (the
worker's post-``astream`` commit in :mod:`executor.graph`) can compose these
helpers into a single transaction alongside the lease-validated
``UPDATE tasks SET status='completed'``.

The caller owns the asyncpg connection and the transaction boundary. Every
function here takes the ``conn`` as its first argument and expects to run
inside an active transaction. That mirrors how the existing cost-ledger
helpers in :mod:`executor.graph` compose with the lease-check path.

Design invariants enforced here:

* **UPSERT on ``task_id``** — follow-up and redrive reuse the same task id, so
  the insert uses ``ON CONFLICT (task_id) DO UPDATE``. ``created_at`` is
  immutable; ``updated_at`` and ``version`` advance.
* **INSERT vs UPDATE signal** — :func:`upsert_memory_entry` returns a boolean
  ``inserted`` via the ``xmax = 0`` test (standard Postgres idiom for
  detecting the INSERT branch of ``ON CONFLICT``). The caller uses this to
  gate FIFO trim — trim must only fire on INSERT; UPDATE leaves row count
  unchanged.
* **FIFO trim ordering** — :func:`trim_oldest` orders by ``(created_at ASC,
  memory_id ASC)`` so ties resolve deterministically. The ``keep_memory_id``
  parameter excludes the row that was just inserted from the eviction set,
  guaranteeing a fresh insert cannot evict itself.
* **``max_entries`` clamp** — :func:`max_entries_for_agent` clamps to
  ``[100, 100_000]`` with platform default ``10_000`` per the design doc.

Memory-disabled agents never reach this module — gating happens upstream in
:func:`executor.memory_graph.effective_memory_enabled`.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

import asyncpg

logger = logging.getLogger(__name__)


# Platform defaults (design doc "Validation and Consistency Rules").
DEFAULT_MAX_ENTRIES = 10_000
MIN_MAX_ENTRIES = 100
MAX_MAX_ENTRIES = 100_000


def max_entries_for_agent(agent_config: dict[str, Any] | None) -> int:
    """Resolve ``agent_config.memory.max_entries`` with platform-level clamps.

    * Missing, ``None``, or non-integer → platform default ``10_000``.
    * Below the floor → clamp up to ``100``.
    * Above the ceiling → clamp down to ``100_000``.

    Matches the validation rule enforced on the API side so the worker and
    the API agree on the effective cap even if an older agent row slipped
    past pre-Track-5 validation.
    """
    if not isinstance(agent_config, dict):
        return DEFAULT_MAX_ENTRIES
    memory_section = agent_config.get("memory")
    if not isinstance(memory_section, dict):
        return DEFAULT_MAX_ENTRIES
    raw = memory_section.get("max_entries")
    if raw is None or isinstance(raw, bool) or not isinstance(raw, int):
        return DEFAULT_MAX_ENTRIES
    if raw < MIN_MAX_ENTRIES:
        return MIN_MAX_ENTRIES
    if raw > MAX_MAX_ENTRIES:
        return MAX_MAX_ENTRIES
    return int(raw)


# SQL ---------------------------------------------------------------------

# pgvector does not automatically cast a Postgres array parameter to the
# ``vector`` type, so we cast explicitly in the statement. The caller passes
# the vector as an asyncpg-compatible representation (list serialised as a
# JSON-style ``"[...]"`` string — see _vec_to_sql_literal below). For NULL
# embeddings the caller passes ``None`` and the cast is a no-op.
_UPSERT_SQL = """
INSERT INTO agent_memory_entries (
    tenant_id, agent_id, task_id,
    title, summary, observations, outcome, tags,
    content_vec, summarizer_model_id
) VALUES (
    $1, $2, $3::uuid,
    $4, $5, $6::text[], $7, $8::text[],
    CASE WHEN $9::text IS NULL THEN NULL ELSE $9::text::vector END,
    $10
)
ON CONFLICT (task_id) DO UPDATE SET
    title               = EXCLUDED.title,
    summary             = EXCLUDED.summary,
    observations        = EXCLUDED.observations,
    outcome             = EXCLUDED.outcome,
    tags                = EXCLUDED.tags,
    content_vec         = EXCLUDED.content_vec,
    summarizer_model_id = EXCLUDED.summarizer_model_id,
    version             = agent_memory_entries.version + 1,
    updated_at          = NOW()
RETURNING memory_id, (xmax = 0) AS inserted
"""

_TRIM_SQL = """
WITH excess AS (
    -- Order NEWEST first, skip the first ``non_keep_cap`` rows, and mark
    -- anything after the skip as the eviction set. The remaining non-keep
    -- population is therefore the N youngest rows, which together with the
    -- ``keep`` row makes exactly ``max_entries`` rows.
    SELECT memory_id
    FROM agent_memory_entries
    WHERE tenant_id = $1
      AND agent_id = $2
      AND memory_id <> $3
    ORDER BY created_at DESC, memory_id DESC
    OFFSET $4
)
DELETE FROM agent_memory_entries
WHERE memory_id IN (SELECT memory_id FROM excess)
RETURNING memory_id
"""

_COUNT_SQL = """
SELECT COUNT(*) FROM agent_memory_entries
WHERE tenant_id = $1 AND agent_id = $2
"""


def _vec_to_sql_literal(vec: list[float] | None) -> str | None:
    """Render a vector as the pgvector-accepted text literal ``"[a, b, c]"``.

    We pass it as a Postgres TEXT parameter and cast it to ``vector`` in the
    SQL above. This sidesteps asyncpg not knowing about pgvector types out
    of the box, without pulling in a pgvector-specific driver extension.
    """
    if vec is None:
        return None
    return "[" + ",".join(f"{float(v):.7f}" for v in vec) + "]"


async def upsert_memory_entry(
    conn: asyncpg.Connection,
    entry: dict[str, Any],
) -> dict[str, Any]:
    """Upsert one row into ``agent_memory_entries``.

    Parameters
    ----------
    conn:
        asyncpg connection owned by the caller and already inside a
        transaction. On rollback the memory row is rolled back with it.
    entry:
        Dict with keys ``tenant_id``, ``agent_id``, ``task_id``, ``title``,
        ``summary``, ``observations`` (list[str]), ``outcome``
        (``'succeeded'`` / ``'failed'``), ``tags`` (list[str]),
        ``content_vec`` (list[float] | None), and ``summarizer_model_id``.

    Returns
    -------
    ``{"memory_id": UUID, "inserted": bool}`` — ``inserted`` is ``True`` on
    the INSERT branch and ``False`` on the UPDATE branch (per the ``xmax=0``
    idiom). Callers use this to gate FIFO trim.
    """
    row = await conn.fetchrow(
        _UPSERT_SQL,
        entry["tenant_id"],
        entry["agent_id"],
        str(entry["task_id"]),
        entry["title"],
        entry["summary"],
        list(entry.get("observations") or []),
        entry["outcome"],
        list(entry.get("tags") or []),
        _vec_to_sql_literal(entry.get("content_vec")),
        entry.get("summarizer_model_id"),
    )
    if row is None:
        # Unreachable — the RETURNING clause always produces a row on a
        # successful INSERT/UPDATE. Defensive fallback keeps the type
        # contract honest.
        raise RuntimeError("UPSERT returned no row; should be unreachable")
    return {"memory_id": row["memory_id"], "inserted": bool(row["inserted"])}


async def count_entries_for_agent(
    conn: asyncpg.Connection,
    tenant_id: str,
    agent_id: str,
) -> int:
    """Count ``agent_memory_entries`` scoped by ``(tenant_id, agent_id)``.

    Used by the commit path to decide whether a fresh INSERT tipped the agent
    past ``max_entries``. Runs inside the same transaction as the INSERT so
    the count includes the just-inserted row.
    """
    value = await conn.fetchval(_COUNT_SQL, tenant_id, agent_id)
    return int(value or 0)


async def trim_oldest(
    conn: asyncpg.Connection,
    *,
    tenant_id: str,
    agent_id: str,
    max_entries: int,
    keep_memory_id: uuid.UUID,
) -> int:
    """Evict the oldest memory rows for an agent that exceed ``max_entries``.

    Parameters
    ----------
    conn:
        asyncpg connection, inside the same transaction as the INSERT that
        raised the row count past the cap.
    tenant_id, agent_id:
        Scope of the trim.
    max_entries:
        Target cap. The **non-keep** rows are ordered oldest-first; anything
        beyond ``max_entries - 1`` rows (we always keep the fresh row) is
        deleted.
    keep_memory_id:
        The ``memory_id`` of the row we just inserted. Excluded from the
        eviction candidate set unconditionally — guarantees a fresh write
        cannot evict itself even in pathological clock-skew scenarios.

    Returns
    -------
    Count of rows evicted (``0`` if no trim was needed).
    """
    # The "keep" row counts toward the cap, so the OFFSET applied to the
    # non-keep set is ``max_entries - 1``. If ``max_entries`` is 0 or less
    # (should never happen thanks to the clamp) we behave as if the cap is
    # 1 — the keep row is preserved and everything else is evicted.
    non_keep_cap = max(0, max_entries - 1)
    deleted_rows = await conn.fetch(
        _TRIM_SQL,
        tenant_id, agent_id, keep_memory_id, non_keep_cap,
    )
    evicted = len(deleted_rows)
    if evicted > 0:
        logger.info(
            "memory.write.trim_evicted tenant_id=%s agent_id=%s count=%d",
            tenant_id, agent_id, evicted,
        )
    return evicted


def read_pending_memory_from_state_values(
    values: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Read ``pending_memory`` out of a LangGraph ``values`` dict.

    Exists as a standalone helper so the commit path in :mod:`executor.graph`
    and the (eventual) dead-letter hook in Task 8 can both share a single
    read rule. ``values`` comes from ``CompiledGraph.aget_state(config).values``.
    """
    if not isinstance(values, dict):
        return None
    pending = values.get("pending_memory")
    if not isinstance(pending, dict):
        return None
    return pending


async def read_pending_memory_from_checkpoint(
    checkpointer,
    config: dict[str, Any],
) -> dict[str, Any] | None:
    """Read ``pending_memory`` directly from the latest checkpoint.

    Alternative read path used by the dead-letter / re-claim hooks in Task 8.
    The ``aget_tuple(config)`` checkpoint has the state dict nested under
    ``checkpoint['channel_values']``; we honour that shape and return ``None``
    if any step of the lookup fails. Never raises.
    """
    try:
        tup = await checkpointer.aget_tuple(config)
        if tup is None:
            return None
        checkpoint = getattr(tup, "checkpoint", None) or {}
        values = checkpoint.get("channel_values") if isinstance(checkpoint, dict) else None
        return read_pending_memory_from_state_values(values)
    except Exception:
        logger.warning("read_pending_memory_from_checkpoint failed", exc_info=True)
        return None


# Convenience helper so the caller can JSON-encode a row's ``pending_memory``
# cleanly when logging. Keeps logging cheap and non-destructive.
def pending_memory_log_preview(pending_memory: dict[str, Any]) -> str:
    preview = {
        "title": pending_memory.get("title"),
        "summarizer_model_id": pending_memory.get("summarizer_model_id"),
        "outcome": pending_memory.get("outcome"),
        "observations_count": len(pending_memory.get("observations_snapshot") or []),
        "content_vec_null": pending_memory.get("content_vec") is None,
    }
    return json.dumps(preview, default=str)
