"""Phase 2 Track 7 Task 13 — append-only conversation-log repository.

This module is the Python side of the user-facing conversation log. The
Console reads the same ``task_conversation_log`` table via the Java API
service; the Python repository is intentionally **write-only** — no
``list_entries``, no mutation methods, no ``mark_superseded``.

Write-path contract (from the task-13 spec):

* Every insert is keyed by ``(task_id, idempotency_key)`` with
  ``ON CONFLICT DO NOTHING``. A retry with the same key is a no-op and
  returns ``None``. Callers treat ``None`` as "don't depend on this" —
  the log is best-effort audit, not transactional truth.
* Appends never raise. DB errors are logged via the structured logger
  ``conversation_log.append_failed`` and counted on
  ``conversation_log_append_failed_total`` labeled by ``kind`` and
  ``exception_class``. The caller (``agent_node``) continues unaffected.
* Tool-call ``args`` are serialized at the call site via
  ``json.dumps(args, default=str)``. This repository takes the already-
  shaped ``content`` dict — no re-serialization of nested structures.
* ``content_size`` is computed from ``json.dumps(content)`` at insert time
  so the Console and ops dashboards can report byte volume without a
  JSONB scan.

The repository is crash-retry-safe. ``content_version`` defaults to 1 for
v1; Phase 3+ bumps to 2 when blob-offload or rollback columns land.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from typing import Any, Literal

import asyncpg
import structlog

logger = logging.getLogger(__name__)
_struct_logger = structlog.get_logger(__name__)


# Literal of valid ``kind`` values. Mirrors the CHECK constraint in
# migration 0017 — updating either side requires updating the other via
# the Track 2 DROP+ADD pattern.
ConversationLogKind = Literal[
    "user_turn",
    "agent_turn",
    "tool_call",
    "tool_result",
    "system_note",
    "compaction_boundary",
    "memory_flush",
    "hitl_pause",
    "hitl_resume",
    # Track 7 Follow-up Task 5 — one entry per ingestion-offload pass that
    # moved ≥1 tool result / arg to S3. Payload: {count, total_bytes,
    # step_index}. Emission is best-effort (same contract as every other
    # kind) and does not block the agent super-step.
    "offload_emitted",
]


_VALID_KINDS: frozenset[str] = frozenset(
    {
        "user_turn",
        "agent_turn",
        "tool_call",
        "tool_result",
        "system_note",
        "compaction_boundary",
        "memory_flush",
        "hitl_pause",
        "hitl_resume",
        "offload_emitted",
    }
)


# Process-local metric counters. In the worker's deployment this is the
# append-only audit trail — a sustained spike is the ops signal that the
# log store is degraded. The counter is keyed by (kind, exception_class)
# as required by the task-13 spec failure envelope section.
#
# Kept in-process (not wired into structlog / OTEL) because the worker
# does not yet have a Prometheus / OTEL exporter. Tests read it directly.
_append_failed_counter: dict[tuple[str, str], int] = {}
_append_failed_counter_lock = asyncio.Lock()


def get_append_failed_counter(kind: str, exception_class: str) -> int:
    """Return the current value of ``conversation_log_append_failed_total``.

    Keyed by ``(kind, exception_class)`` per the spec. Tests use this to
    assert the counter advanced without tying to a specific OTEL surface.
    """
    return _append_failed_counter.get((kind, exception_class), 0)


def _record_append_failure(kind: str, exception_class: str) -> None:
    """Increment the append-failure counter (non-async; called on the hot path)."""
    key = (kind, exception_class)
    _append_failed_counter[key] = _append_failed_counter.get(key, 0) + 1


def reset_append_failed_counter() -> None:
    """Zero the counter. Test-only; never called from production code."""
    _append_failed_counter.clear()


def compute_idempotency_key(
    *,
    task_id: str,
    checkpoint_id: str | None,
    origin_ref: str,
) -> str:
    """Compute ``sha256(task_id || (checkpoint_id or "init") || origin_ref)``.

    The ``"init"`` literal substitutes for missing ``checkpoint_id`` so the
    first-turn key is stable even before LangGraph persists a checkpoint.
    ``origin_ref`` is the LangGraph message id for model/tool messages, or
    a deterministic compaction-event id (e.g.,
    ``"tier3:<watermark_before>->{watermark_after}"``) for compaction
    entries.

    The hex digest is returned verbatim so callers can store and log it.
    """
    checkpoint_part = checkpoint_id if checkpoint_id is not None else "init"
    material = f"{task_id}|{checkpoint_part}|{origin_ref}".encode("utf-8")
    return hashlib.sha256(material).hexdigest()


_INSERT_SQL = """
INSERT INTO task_conversation_log (
    tenant_id,
    task_id,
    checkpoint_id,
    idempotency_key,
    kind,
    role,
    content_version,
    content,
    content_size,
    metadata
) VALUES (
    $1,
    $2::uuid,
    $3,
    $4,
    $5,
    $6,
    $7,
    $8::jsonb,
    $9,
    $10::jsonb
)
ON CONFLICT (task_id, idempotency_key) DO NOTHING
RETURNING sequence
"""


def _encode_json(payload: dict[str, Any] | None) -> str:
    """Canonical-ish JSON encoding. ``default=str`` handles datetime / UUID / Path."""
    if payload is None:
        return "{}"
    return json.dumps(payload, default=str, sort_keys=False, ensure_ascii=False)


class ConversationLogRepository:
    """Append-only repository for the user-facing conversation log.

    Instantiated once per :class:`GraphExecutor` (shares the executor's
    asyncpg pool). The only public method is :meth:`append_entry`.
    """

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def append_entry(
        self,
        *,
        task_id: str,
        tenant_id: str,
        checkpoint_id: str | None,
        idempotency_key: str,
        kind: ConversationLogKind,
        role: str | None,
        content: dict[str, Any],
        metadata: dict[str, Any] | None = None,
        content_version: int = 1,
    ) -> int | None:
        """Insert one entry. Return the assigned ``sequence`` or ``None``.

        ``None`` means either the idempotency key collided (retry / crash-
        resume path swallowed by ``ON CONFLICT DO NOTHING``) OR the write
        failed at the DB level. Callers treat ``None`` as
        "maybe wrote, maybe dedup'd, maybe failed — don't depend on this".

        On any ``asyncpg`` / connection failure the repository:

        1. Logs at WARN via ``conversation_log.append_failed`` with the
           fields required by the task-13 spec.
        2. Increments ``conversation_log_append_failed_total`` labeled by
           ``(kind, exception_class)``.
        3. Returns ``None``. **Never raises.**
        """
        if kind not in _VALID_KINDS:
            # This is a programmer error (bad literal) — surface it loudly
            # in logs but still do not raise, so the graph step proceeds.
            _struct_logger.warning(
                "conversation_log.append_failed",
                task_id=task_id,
                tenant_id=tenant_id,
                checkpoint_id=checkpoint_id,
                idempotency_key=idempotency_key,
                kind=kind,
                exception_class="InvalidKind",
                exception_message=f"Unknown kind: {kind!r}",
            )
            _record_append_failure(str(kind), "InvalidKind")
            return None

        content_json = _encode_json(content)
        metadata_json = _encode_json(metadata)
        # content_size is serialized bytes (UTF-8). Matches the spec —
        # Console / ops dashboards display this without a jsonb scan.
        content_size = len(content_json.encode("utf-8"))

        try:
            async with self._pool.acquire() as conn:
                row = await conn.fetchrow(
                    _INSERT_SQL,
                    tenant_id,
                    str(task_id),
                    checkpoint_id,
                    idempotency_key,
                    kind,
                    role,
                    int(content_version),
                    content_json,
                    content_size,
                    metadata_json,
                )
        except Exception as exc:  # noqa: BLE001 — best-effort envelope
            exc_class = type(exc).__name__
            _struct_logger.warning(
                "conversation_log.append_failed",
                task_id=task_id,
                tenant_id=tenant_id,
                checkpoint_id=checkpoint_id,
                idempotency_key=idempotency_key,
                kind=kind,
                exception_class=exc_class,
                exception_message=str(exc)[:500],
            )
            _record_append_failure(str(kind), exc_class)
            return None

        if row is None:
            # ON CONFLICT DO NOTHING fired — dedup hit. Not an error.
            return None
        return int(row["sequence"])
