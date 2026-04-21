"""Phase 2 Track 7 Follow-up Task 8 (D) — Convlog → task_events backfill CLI.

One-shot operational command that copies user-visible markers from
``task_conversation_log`` into ``task_events`` for in-flight tasks that
existed BEFORE the Task 8 worker-side dual-write landed. The Activity
projection reads from ``task_events``; in-flight tasks that pre-date the
flag flip would otherwise silently drop their pre-flip marker history.

Scope (see §Backfill in task-8-unify-conversation-timeline-design.md):

* Non-terminal tasks only. The enum of terminal statuses is derived from
  the SQL ``tasks_status_check`` constraint at runtime — we *exclude*
  ``completed`` and ``dead_letter`` and let anything else count as
  non-terminal. Historical/completed tasks keep working through the
  legacy Conversation pane during Phases A–C; after Phase D they render
  from checkpoints only.
* Three convlog kinds are backfilled:
      ``memory_flush``    → task_events.event_type = 'memory_flush'
      ``system_note``     → task_events.event_type = 'system_note'
      ``offload_emitted`` → task_events.event_type = 'offload_emitted'
  The other convlog kinds map to events the worker or API already emits
  (``compaction_boundary`` → ``task_compaction_fired`` dual-write lands
  in Task 8 A; ``hitl_pause/hitl_resume`` → existing lifecycle events
  emitted atomically with state transitions).

Idempotence (see design §Backfill):

* ``details.backfill_key = sha256(task_id || convlog_row_id || event_type)``.
  Repeated runs are safe — the CLI skips rows whose backfill_key already
  appears in ``task_events.details``. The conservative hash covers both
  (same-millisecond event dedup) and (idempotent re-runs after a partial
  failure).

Usage::

    services/worker-service/.venv/bin/python -m scripts.backfill_convlog_to_task_events \
        --dsn "$DATABASE_URL" \
        --dry-run

Flags:
    --dsn       Postgres DSN (falls back to ``$DATABASE_URL``, then
                ``$E2E_DB_DSN`` for local test runs).
    --dry-run   Log the per-row plan without writing. Useful before
                promoting against production.
    --tenant    Optional tenant filter (default: all tenants).
    --limit     Per-task convlog row cap (default: 10_000). Prevents
                runaway scans on pathologically long logs.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import sys
from typing import Any, Iterable

import asyncpg


logger = logging.getLogger("backfill_convlog_to_task_events")


# Convlog kinds that the Activity projection reads from ``task_events``.
# Other convlog kinds either map to events the worker/API already emits
# (compaction_boundary / hitl_*) or are message-stream payloads that live
# in checkpoints (user_turn / agent_turn / tool_call / tool_result).
_KIND_TO_EVENT_TYPE: dict[str, str] = {
    "memory_flush": "memory_flush",
    "system_note": "system_note",
    "offload_emitted": "offload_emitted",
}


def _terminal_statuses() -> set[str]:
    """Return the hard-coded terminal set.

    Kept as a function (not a module-level constant) so tests can
    monkey-patch it and so a future schema change that introduces a new
    terminal state produces a single clear diff site rather than a
    silent behavior change scattered across the CLI.
    """
    return {"completed", "dead_letter"}


def _backfill_key(task_id: str, convlog_entry_id: str, event_type: str) -> str:
    """Deterministic sha256(task_id || entry_id || event_type)."""
    material = f"{task_id}|{convlog_entry_id}|{event_type}".encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def _merged_details(
    *,
    convlog_content: Any,
    convlog_metadata: Any,
    backfill_key: str,
) -> dict[str, Any]:
    """Build the task_events.details JSON for a backfilled row.

    Preserves the convlog payload verbatim under ``content`` and
    ``metadata`` keys so the projection renders the same body as a
    natively-emitted task_event. Adds provenance flags
    (``backfilled_from_convlog`` + ``backfill_key``) for auditability
    and idempotence.
    """
    details: dict[str, Any] = {
        "backfilled_from_convlog": True,
        "backfill_key": backfill_key,
    }
    if isinstance(convlog_content, dict):
        details.update(convlog_content)
    elif convlog_content is not None:
        details["content"] = convlog_content
    if isinstance(convlog_metadata, dict) and convlog_metadata:
        # Merge non-overlapping metadata. If a metadata key collides with
        # a content key (rare in practice), content wins — it's the
        # authoritative payload per Task 13's contract.
        for k, v in convlog_metadata.items():
            details.setdefault(k, v)
    return details


def _as_dict(value: Any) -> Any:
    """Normalise asyncpg's JSONB return (str or dict) to a dict."""
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return None
    return None


async def _select_target_tasks(
    conn: asyncpg.Connection, *, tenant: str | None
) -> Iterable[asyncpg.Record]:
    terminal = _terminal_statuses()
    sql = "SELECT task_id, tenant_id, agent_id FROM tasks WHERE status <> ALL($1::text[])"
    args: list[Any] = [list(terminal)]
    if tenant is not None:
        sql += " AND tenant_id = $2"
        args.append(tenant)
    return await conn.fetch(sql, *args)


async def _select_convlog_rows(
    conn: asyncpg.Connection,
    *,
    task_id: Any,
    kinds: list[str],
    limit: int,
) -> Iterable[asyncpg.Record]:
    sql = """
        SELECT entry_id, kind, content, metadata, created_at
          FROM task_conversation_log
         WHERE task_id = $1::uuid
           AND kind = ANY($2::text[])
         ORDER BY sequence
         LIMIT $3
        """
    return await conn.fetch(sql, task_id, kinds, limit)


async def _backfill_key_exists(
    conn: asyncpg.Connection, *, task_id: Any, backfill_key: str
) -> bool:
    row = await conn.fetchval(
        """
        SELECT 1 FROM task_events
         WHERE task_id = $1::uuid
           AND details->>'backfill_key' = $2
         LIMIT 1
        """,
        task_id,
        backfill_key,
    )
    return row is not None


async def _insert_backfilled_event(
    conn: asyncpg.Connection,
    *,
    tenant_id: str,
    task_id: Any,
    agent_id: str,
    event_type: str,
    details: dict[str, Any],
    created_at: Any,
) -> None:
    await conn.execute(
        """
        INSERT INTO task_events (tenant_id, task_id, agent_id, event_type,
                                 status_before, status_after, worker_id,
                                 error_code, error_message, details, created_at)
        VALUES ($1, $2::uuid, $3, $4, NULL, NULL, 'backfill', NULL, NULL,
                $5::jsonb, $6)
        """,
        tenant_id,
        task_id,
        agent_id,
        event_type,
        json.dumps(details),
        created_at,
    )


async def backfill(
    *,
    dsn: str,
    dry_run: bool,
    tenant: str | None,
    limit: int,
) -> dict[str, int]:
    """Run the backfill and return per-action counters.

    Returns a dict with keys: ``tasks``, ``rows_scanned``, ``rows_inserted``,
    ``rows_skipped_dedup``, ``rows_skipped_unknown_kind``. The dict is
    logged at INFO and returned to the CLI main for exit-status gating.
    """
    stats = {
        "tasks": 0,
        "rows_scanned": 0,
        "rows_inserted": 0,
        "rows_skipped_dedup": 0,
        "rows_skipped_unknown_kind": 0,
    }
    kinds = list(_KIND_TO_EVENT_TYPE.keys())
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=4)
    try:
        async with pool.acquire() as conn:
            tasks = await _select_target_tasks(conn, tenant=tenant)
        for task_row in tasks:
            task_id = task_row["task_id"]
            tenant_id = task_row["tenant_id"]
            agent_id = task_row["agent_id"]
            stats["tasks"] += 1
            async with pool.acquire() as conn:
                convlog_rows = await _select_convlog_rows(
                    conn, task_id=task_id, kinds=kinds, limit=limit
                )
                for row in convlog_rows:
                    stats["rows_scanned"] += 1
                    kind = row["kind"]
                    event_type = _KIND_TO_EVENT_TYPE.get(kind)
                    if event_type is None:
                        stats["rows_skipped_unknown_kind"] += 1
                        continue
                    backfill_key = _backfill_key(
                        str(task_id), str(row["entry_id"]), event_type
                    )
                    if await _backfill_key_exists(
                        conn, task_id=task_id, backfill_key=backfill_key
                    ):
                        stats["rows_skipped_dedup"] += 1
                        continue
                    details = _merged_details(
                        convlog_content=_as_dict(row["content"]),
                        convlog_metadata=_as_dict(row["metadata"]),
                        backfill_key=backfill_key,
                    )
                    if dry_run:
                        logger.info(
                            "backfill.plan task_id=%s event_type=%s backfill_key=%s",
                            task_id, event_type, backfill_key[:12],
                        )
                        stats["rows_inserted"] += 1
                        continue
                    await _insert_backfilled_event(
                        conn,
                        tenant_id=tenant_id,
                        task_id=task_id,
                        agent_id=agent_id,
                        event_type=event_type,
                        details=details,
                        created_at=row["created_at"],
                    )
                    stats["rows_inserted"] += 1
    finally:
        await pool.close()
    return stats


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--dsn", default=None,
                   help="Postgres DSN; falls back to DATABASE_URL or E2E_DB_DSN.")
    p.add_argument("--dry-run", action="store_true",
                   help="Log planned inserts without writing.")
    p.add_argument("--tenant", default=None,
                   help="Restrict to a single tenant_id.")
    p.add_argument("--limit", type=int, default=10_000,
                   help="Per-task convlog row scan cap (default: 10000).")
    p.add_argument("--log-level", default="INFO",
                   help="Python logging level (default: INFO).")
    return p.parse_args(argv)


def _resolve_dsn(cli_dsn: str | None) -> str:
    if cli_dsn:
        return cli_dsn
    env = os.getenv("DATABASE_URL") or os.getenv("E2E_DB_DSN")
    if not env:
        raise SystemExit(
            "DSN is required: pass --dsn, or set DATABASE_URL / E2E_DB_DSN."
        )
    return env


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-5s %(name)s | %(message)s",
    )
    dsn = _resolve_dsn(args.dsn)
    stats = asyncio.run(
        backfill(
            dsn=dsn,
            dry_run=args.dry_run,
            tenant=args.tenant,
            limit=args.limit,
        )
    )
    logger.info("backfill.done %s", stats)
    return 0


if __name__ == "__main__":
    sys.exit(main())
