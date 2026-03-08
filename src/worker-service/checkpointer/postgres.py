"""Lease-aware LangGraph checkpoint saver backed by the Phase 1 Postgres schema."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Iterator, Sequence
from contextlib import asynccontextmanager
from typing import Any

import asyncpg
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import (
    WRITES_IDX_MAP,
    BaseCheckpointSaver,
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
    get_checkpoint_id,
    get_serializable_checkpoint_metadata,
)
from langgraph.checkpoint.serde.base import SerializerProtocol


LEASE_VALIDATION_QUERY = """
SELECT 1
FROM tasks
WHERE task_id = $1::uuid
  AND tenant_id = $2
  AND status = 'running'
  AND lease_owner = $3
FOR UPDATE
"""

UPSERT_CHECKPOINT_QUERY = """
INSERT INTO checkpoints (
    task_id,
    checkpoint_ns,
    checkpoint_id,
    worker_id,
    parent_checkpoint_id,
    thread_ts,
    parent_ts,
    checkpoint_payload,
    metadata_payload
)
VALUES (
    $1::uuid,
    $2,
    $3,
    $4,
    $5,
    $6,
    (
        SELECT thread_ts
        FROM checkpoints
        WHERE task_id = $1::uuid
          AND checkpoint_ns = $2
          AND checkpoint_id = $5
    ),
    $7::jsonb,
    $8::jsonb
)
ON CONFLICT (task_id, checkpoint_ns, checkpoint_id) DO UPDATE
SET checkpoint_payload = EXCLUDED.checkpoint_payload,
    metadata_payload = EXCLUDED.metadata_payload,
    worker_id = EXCLUDED.worker_id,
    parent_checkpoint_id = EXCLUDED.parent_checkpoint_id,
    thread_ts = EXCLUDED.thread_ts,
    parent_ts = EXCLUDED.parent_ts
"""

UPSERT_CHECKPOINT_WRITES_QUERY = """
INSERT INTO checkpoint_writes (
    task_id,
    checkpoint_ns,
    checkpoint_id,
    task_path,
    idx,
    channel,
    type,
    blob
)
VALUES ($1::uuid, $2, $3, $4, $5, $6, $7, $8)
ON CONFLICT (task_id, checkpoint_ns, checkpoint_id, task_path, idx)
DO UPDATE SET channel = EXCLUDED.channel, type = EXCLUDED.type, blob = EXCLUDED.blob
"""

INSERT_CHECKPOINT_WRITES_QUERY = """
INSERT INTO checkpoint_writes (
    task_id,
    checkpoint_ns,
    checkpoint_id,
    task_path,
    idx,
    channel,
    type,
    blob
)
VALUES ($1::uuid, $2, $3, $4, $5, $6, $7, $8)
ON CONFLICT (task_id, checkpoint_ns, checkpoint_id, task_path, idx)
DO NOTHING
"""

SELECT_CHECKPOINT_BASE = """
SELECT
    task_id::text AS task_id,
    checkpoint_ns,
    checkpoint_id,
    parent_checkpoint_id,
    thread_ts,
    parent_ts,
    checkpoint_payload,
    metadata_payload
FROM checkpoints
"""

SELECT_PENDING_WRITES_QUERY = """
SELECT task_path, channel, type, blob
FROM checkpoint_writes
WHERE task_id = $1::uuid
  AND checkpoint_ns = $2
  AND checkpoint_id = $3
ORDER BY task_path, idx
"""

DELETE_CHECKPOINT_WRITES_QUERY = """
DELETE FROM checkpoint_writes
WHERE task_id = $1::uuid
"""

DELETE_CHECKPOINTS_QUERY = """
DELETE FROM checkpoints
WHERE task_id = $1::uuid
"""


class LeaseRevokedException(RuntimeError):
    """Raised when a worker loses task ownership before a checkpoint write."""


class PostgresDurableCheckpointer(BaseCheckpointSaver[str]):
    """Persist LangGraph checkpoints into the Phase 1 Postgres schema."""

    def __init__(
        self,
        pool_or_conn: asyncpg.Pool | asyncpg.Connection,
        *,
        worker_id: str,
        tenant_id: str,
        serde: SerializerProtocol | None = None,
    ) -> None:
        super().__init__(serde=serde)
        self._db = pool_or_conn
        self._worker_id = worker_id
        self._tenant_id = tenant_id

    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        del new_versions

        thread_id, checkpoint_ns, parent_checkpoint_id = self._extract_checkpoint_target(
            config
        )
        checkpoint_payload = json.dumps(checkpoint)
        metadata_payload = json.dumps(
            get_serializable_checkpoint_metadata(config, metadata)
        )

        async with self._connection() as conn:
            async with conn.transaction():
                lease_ok = await conn.fetchval(
                    LEASE_VALIDATION_QUERY,
                    thread_id,
                    self._tenant_id,
                    self._worker_id,
                )
                if lease_ok is None:
                    raise LeaseRevokedException(
                        f"Lease revoked before checkpoint write for task {thread_id}"
                    )

                await conn.execute(
                    UPSERT_CHECKPOINT_QUERY,
                    thread_id,
                    checkpoint_ns,
                    checkpoint["id"],
                    self._worker_id,
                    parent_checkpoint_id,
                    checkpoint["ts"],
                    checkpoint_payload,
                    metadata_payload,
                )

        return {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_ns": checkpoint_ns,
                "checkpoint_id": checkpoint["id"],
            }
        }

    async def aput_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        # The Phase 1 schema links writes to the root task/thread via the checkpoint FK.
        # The upstream write-scoped task_id is not persisted separately in this schema.
        del task_id
        thread_id, checkpoint_ns, checkpoint_id = self._extract_checkpoint_target(config)
        if checkpoint_id is None:
            raise ValueError("checkpoint_id is required for put_writes()")

        query = (
            UPSERT_CHECKPOINT_WRITES_QUERY
            if all(channel in WRITES_IDX_MAP for channel, _ in writes)
            else INSERT_CHECKPOINT_WRITES_QUERY
        )
        params = [
            (
                thread_id,
                checkpoint_ns,
                checkpoint_id,
                task_path,
                WRITES_IDX_MAP.get(channel, idx),
                channel,
                *self.serde.dumps_typed(value),
            )
            for idx, (channel, value) in enumerate(writes)
        ]

        if not params:
            return

        async with self._connection() as conn:
            await conn.executemany(query, params)

    async def aget_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        thread_id, checkpoint_ns, checkpoint_id = self._extract_checkpoint_target(config)
        if checkpoint_id:
            query = (
                SELECT_CHECKPOINT_BASE
                + """
WHERE task_id = $1::uuid
  AND checkpoint_ns = $2
  AND checkpoint_id = $3
"""
            )
            args: tuple[Any, ...] = (thread_id, checkpoint_ns, checkpoint_id)
        else:
            query = (
                SELECT_CHECKPOINT_BASE
                + """
WHERE task_id = $1::uuid
  AND checkpoint_ns = $2
ORDER BY checkpoint_id DESC
LIMIT 1
"""
            )
            args = (thread_id, checkpoint_ns)

        async with self._connection() as conn:
            row = await conn.fetchrow(query, *args)
            if row is None:
                return None

            return await self._row_to_checkpoint_tuple(conn, row)

    async def alist(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> AsyncIterator[CheckpointTuple]:
        query_parts = [SELECT_CHECKPOINT_BASE]
        where_clauses: list[str] = []
        args: list[Any] = []

        if config is not None:
            configurable = config.get("configurable", {})
            args.append(str(configurable["thread_id"]))
            where_clauses.append(f"task_id = ${len(args)}::uuid")

            checkpoint_ns = configurable.get("checkpoint_ns")
            if checkpoint_ns is not None:
                args.append(checkpoint_ns)
                where_clauses.append(f"checkpoint_ns = ${len(args)}")

            checkpoint_id = get_checkpoint_id(config)
            if checkpoint_id is not None:
                args.append(checkpoint_id)
                where_clauses.append(f"checkpoint_id = ${len(args)}")

        if filter:
            args.append(json.dumps(filter))
            where_clauses.append(f"metadata_payload @> ${len(args)}::jsonb")

        if before is not None:
            before_checkpoint_id = get_checkpoint_id(before)
            if before_checkpoint_id is not None:
                args.append(before_checkpoint_id)
                where_clauses.append(f"checkpoint_id < ${len(args)}")

        if where_clauses:
            query_parts.append("WHERE " + " AND ".join(where_clauses))

        query_parts.append("ORDER BY checkpoint_id DESC")
        if limit is not None:
            args.append(int(limit))
            query_parts.append(f"LIMIT ${len(args)}")

        async with self._connection() as conn:
            rows = await conn.fetch("\n".join(query_parts), *args)
            for row in rows:
                yield await self._row_to_checkpoint_tuple(conn, row)

    async def adelete_thread(self, thread_id: str) -> None:
        async with self._connection() as conn:
            async with conn.transaction():
                await conn.execute(DELETE_CHECKPOINT_WRITES_QUERY, str(thread_id))
                await conn.execute(DELETE_CHECKPOINTS_QUERY, str(thread_id))

    def get_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        return self._run_sync(self.aget_tuple(config))

    def list(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> Iterator[CheckpointTuple]:
        for item in self._run_sync(self._collect_list(config, filter, before, limit)):
            yield item

    def put(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        return self._run_sync(self.aput(config, checkpoint, metadata, new_versions))

    def put_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        self._run_sync(self.aput_writes(config, writes, task_id, task_path))

    def delete_thread(self, thread_id: str) -> None:
        self._run_sync(self.adelete_thread(thread_id))

    def delete_for_runs(self, *args: Any, **kwargs: Any) -> None:
        raise NotImplementedError("delete_for_runs is not implemented in Phase 1")

    def copy_thread(self, *args: Any, **kwargs: Any) -> None:
        raise NotImplementedError("copy_thread is not implemented in Phase 1")

    def prune(self, *args: Any, **kwargs: Any) -> None:
        raise NotImplementedError("prune is not implemented in Phase 1")

    async def _collect_list(
        self,
        config: RunnableConfig | None,
        filter: dict[str, Any] | None,
        before: RunnableConfig | None,
        limit: int | None,
    ) -> list[CheckpointTuple]:
        return [
            item
            async for item in self.alist(
                config,
                filter=filter,
                before=before,
                limit=limit,
            )
        ]

    def _extract_checkpoint_target(
        self, config: RunnableConfig
    ) -> tuple[str, str, str | None]:
        configurable = config["configurable"]
        thread_id = str(configurable["thread_id"])
        checkpoint_ns = configurable.get("checkpoint_ns") or ""
        checkpoint_id = get_checkpoint_id(config)
        return thread_id, checkpoint_ns, checkpoint_id

    async def _row_to_checkpoint_tuple(
        self,
        conn: asyncpg.Connection,
        row: asyncpg.Record,
    ) -> CheckpointTuple:
        task_id = str(row["task_id"])
        checkpoint_ns = row["checkpoint_ns"]
        checkpoint_id = row["checkpoint_id"]
        pending_rows = await conn.fetch(
            SELECT_PENDING_WRITES_QUERY,
            task_id,
            checkpoint_ns,
            checkpoint_id,
        )

        checkpoint_payload = self._coerce_json(row["checkpoint_payload"])
        metadata_payload = self._coerce_json(row["metadata_payload"])

        # The Phase 1 schema stores task_path but not the upstream writer task ID.
        # We surface task_path in the first pending write slot to preserve a stable
        # ordering key for recovery.
        pending_writes = [
            (
                pending_row["task_path"],
                pending_row["channel"],
                self.serde.loads_typed((pending_row["type"], pending_row["blob"])),
            )
            for pending_row in pending_rows
        ]

        parent_checkpoint_id = row["parent_checkpoint_id"]
        parent_config = None
        if parent_checkpoint_id:
            parent_config = {
                "configurable": {
                    "thread_id": task_id,
                    "checkpoint_ns": checkpoint_ns,
                    "checkpoint_id": parent_checkpoint_id,
                }
            }

        return CheckpointTuple(
            config={
                "configurable": {
                    "thread_id": task_id,
                    "checkpoint_ns": checkpoint_ns,
                    "checkpoint_id": checkpoint_id,
                }
            },
            checkpoint=checkpoint_payload,
            metadata=metadata_payload,
            parent_config=parent_config,
            pending_writes=pending_writes,
        )

    def _coerce_json(self, value: Any) -> dict[str, Any]:
        if value is None:
            return {}
        if isinstance(value, str):
            return json.loads(value)
        return dict(value)

    @asynccontextmanager
    async def _connection(self) -> AsyncIterator[asyncpg.Connection]:
        if isinstance(self._db, asyncpg.Connection):
            yield self._db
            return

        async with self._db.acquire() as conn:
            yield conn

    def _run_sync(self, coroutine: Any) -> Any:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(coroutine)
        raise asyncio.InvalidStateError(
            "Synchronous checkpointer methods cannot be called from a running event loop."
        )
