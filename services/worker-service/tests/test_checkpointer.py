"""Tests for the lease-aware Postgres checkpointer."""

from __future__ import annotations

import json
from typing import Any

import pytest
from langgraph.checkpoint.base import WRITES_IDX_MAP

from checkpointer.postgres import (
    INSERT_CHECKPOINT_WRITES_QUERY,
    LEASE_VALIDATION_QUERY,
    UPSERT_CHECKPOINT_QUERY,
    LeaseRevokedException,
    PostgresDurableCheckpointer,
)


def _sample_config(
    *,
    checkpoint_id: str | None = "checkpoint-parent",
    checkpoint_ns: str = "",
) -> dict[str, Any]:
    configurable: dict[str, Any] = {
        "thread_id": "00000000-0000-0000-0000-000000000123",
        "checkpoint_ns": checkpoint_ns,
    }
    if checkpoint_id is not None:
        configurable["checkpoint_id"] = checkpoint_id
    return {"configurable": configurable}


def _sample_checkpoint(checkpoint_id: str = "checkpoint-002") -> dict[str, Any]:
    return {
        "v": 1,
        "id": checkpoint_id,
        "ts": "2026-03-07T10:00:01.123456+00:00",
        "channel_values": {"messages": ["hello"], "count": 2},
        "channel_versions": {"messages": "2", "count": "2"},
        "versions_seen": {"agent": {"messages": "1", "count": "1"}},
        "updated_channels": ["messages", "count"],
    }


class _FakeTransaction:
    def __init__(self, conn: "_FakeConnection") -> None:
        self._conn = conn

    async def __aenter__(self) -> "_FakeTransaction":
        self._conn.calls.append(("transaction_enter",))
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        self._conn.calls.append(("transaction_exit", exc_type))
        return False


class _FakeAcquireContext:
    def __init__(self, conn: "_FakeConnection") -> None:
        self._conn = conn

    async def __aenter__(self) -> "_FakeConnection":
        return self._conn

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False


class _FakePool:
    def __init__(self, conn: "_FakeConnection") -> None:
        self._conn = conn

    def acquire(self) -> _FakeAcquireContext:
        return _FakeAcquireContext(self._conn)


class _FakeConnection:
    def __init__(
        self,
        *,
        fetchval_result: Any = 1,
        fetchrow_result: Any = None,
        fetch_results: list[Any] | None = None,
        pending_writes: list[Any] | None = None,
    ) -> None:
        self.fetchval_result = fetchval_result
        self.fetchrow_result = fetchrow_result
        self.fetch_results = fetch_results or []
        self.pending_writes = pending_writes or []
        self.calls: list[tuple[Any, ...]] = []
        self.executemany_args: tuple[str, list[tuple[Any, ...]]] | None = None

    def transaction(self) -> _FakeTransaction:
        return _FakeTransaction(self)

    async def fetchval(self, query: str, *args: Any) -> Any:
        self.calls.append(("fetchval", query, args))
        return self.fetchval_result

    async def execute(self, query: str, *args: Any) -> str:
        self.calls.append(("execute", query, args))
        return "OK"

    async def executemany(self, query: str, args: list[tuple[Any, ...]]) -> None:
        self.calls.append(("executemany", query, args))
        self.executemany_args = (query, args)

    async def fetchrow(self, query: str, *args: Any) -> Any:
        self.calls.append(("fetchrow", query, args))
        return self.fetchrow_result

    async def fetch(self, query: str, *args: Any) -> list[Any]:
        self.calls.append(("fetch", query, args))
        if "FROM checkpoint_writes" in query:
            return self.pending_writes
        return self.fetch_results


class TestPostgresDurableCheckpointer:
    async def test_aput_validates_lease_before_insert(self) -> None:
        conn = _FakeConnection(fetchval_result=1)
        saver = PostgresDurableCheckpointer(
            _FakePool(conn),
            worker_id="worker-123",
            tenant_id="default",
        )

        next_config = await saver.aput(
            _sample_config(),
            _sample_checkpoint(),
            {"source": "loop", "step": 2},
            {"messages": "2"},
        )

        assert conn.calls[1][0] == "fetchval"
        assert LEASE_VALIDATION_QUERY.strip() in conn.calls[1][1]
        assert conn.calls[2][0] == "execute"
        assert UPSERT_CHECKPOINT_QUERY.strip() in conn.calls[2][1]
        assert next_config["configurable"]["checkpoint_id"] == "checkpoint-002"

        insert_args = conn.calls[2][2]
        assert insert_args[0] == "00000000-0000-0000-0000-000000000123"
        assert insert_args[1] == ""
        assert insert_args[2] == "checkpoint-002"
        assert insert_args[3] == "worker-123"
        assert insert_args[4] == "checkpoint-parent"
        assert json.loads(insert_args[6])["id"] == "checkpoint-002"
        assert json.loads(insert_args[7])["step"] == 2

    async def test_aput_raises_when_lease_revoked(self) -> None:
        conn = _FakeConnection(fetchval_result=None)
        saver = PostgresDurableCheckpointer(
            _FakePool(conn),
            worker_id="worker-123",
            tenant_id="default",
        )

        with pytest.raises(LeaseRevokedException):
            await saver.aput(
                _sample_config(),
                _sample_checkpoint(),
                {"source": "loop", "step": 2},
                {"messages": "2"},
            )

        assert [call[0] for call in conn.calls].count("execute") == 0

    async def test_aput_writes_persists_expected_indices(self) -> None:
        conn = _FakeConnection()
        saver = PostgresDurableCheckpointer(
            _FakePool(conn),
            worker_id="worker-123",
            tenant_id="default",
        )

        writes = [
            ("custom", {"value": 1}),
            ("__interrupt__", {"reason": "pause"}),
        ]
        await saver.aput_writes(
            {
                "configurable": {
                    "thread_id": "00000000-0000-0000-0000-000000000123",
                    "checkpoint_ns": "",
                    "checkpoint_id": "checkpoint-002",
                }
            },
            writes,
            task_id="task-path-id",
            task_path="root/agent",
        )

        assert conn.executemany_args is not None
        query, params = conn.executemany_args
        assert INSERT_CHECKPOINT_WRITES_QUERY.strip() in query
        assert any(call[0] == "fetchval" and LEASE_VALIDATION_QUERY.strip() in call[1] for call in conn.calls)
        assert params[0][5] == 0
        assert params[1][5] == WRITES_IDX_MAP["__interrupt__"]
        assert params[0][3] == "task-path-id"
        assert params[0][4] == "root/agent"

    async def test_aput_writes_raises_when_lease_revoked(self) -> None:
        conn = _FakeConnection(fetchval_result=None)
        saver = PostgresDurableCheckpointer(
            _FakePool(conn),
            worker_id="worker-123",
            tenant_id="default",
        )

        with pytest.raises(LeaseRevokedException):
            await saver.aput_writes(
                {
                    "configurable": {
                        "thread_id": "00000000-0000-0000-0000-000000000123",
                        "checkpoint_ns": "",
                        "checkpoint_id": "checkpoint-002",
                    }
                },
                [("custom", {"value": 1})],
                task_id="writer-task",
                task_path="root/agent",
            )

        assert conn.executemany_args is None

    async def test_aget_tuple_reconstructs_parent_and_pending_writes(self) -> None:
        write_type, write_blob = saver_typed({"value": 1})
        conn = _FakeConnection(
            fetchrow_result={
                "task_id": "00000000-0000-0000-0000-000000000123",
                "checkpoint_ns": "",
                "checkpoint_id": "checkpoint-002",
                "parent_checkpoint_id": "checkpoint-001",
                "thread_ts": "2026-03-07T10:00:01.123456+00:00",
                "parent_ts": "2026-03-07T10:00:00.123456+00:00",
                "checkpoint_payload": _sample_checkpoint(),
                "metadata_payload": {"source": "loop", "step": 2},
            },
            pending_writes=[
                {
                    "writer_task_id": "writer-task",
                    "task_path": "root/agent",
                    "channel": "custom",
                    "type": write_type,
                    "blob": write_blob,
                }
            ],
        )
        saver = PostgresDurableCheckpointer(
            _FakePool(conn),
            worker_id="worker-123",
            tenant_id="default",
        )

        checkpoint_tuple = await saver.aget_tuple(_sample_config(checkpoint_id="checkpoint-002"))

        assert checkpoint_tuple is not None
        assert checkpoint_tuple.parent_config == {
            "configurable": {
                "thread_id": "00000000-0000-0000-0000-000000000123",
                "checkpoint_ns": "",
                "checkpoint_id": "checkpoint-001",
            }
        }
        assert checkpoint_tuple.pending_writes == [
            ("writer-task", "custom", {"value": 1})
        ]

    async def test_alist_applies_before_limit_and_filter(self) -> None:
        conn = _FakeConnection(
            fetch_results=[
                {
                    "task_id": "00000000-0000-0000-0000-000000000123",
                    "checkpoint_ns": "",
                    "checkpoint_id": "checkpoint-002",
                    "parent_checkpoint_id": "checkpoint-001",
                    "thread_ts": "2026-03-07T10:00:01.123456+00:00",
                    "parent_ts": "2026-03-07T10:00:00.123456+00:00",
                    "checkpoint_payload": _sample_checkpoint(),
                    "metadata_payload": {"source": "loop", "step": 2},
                },
                {
                    "task_id": "00000000-0000-0000-0000-000000000123",
                    "checkpoint_ns": "",
                    "checkpoint_id": "checkpoint-001",
                    "parent_checkpoint_id": None,
                    "thread_ts": "2026-03-07T10:00:00.123456+00:00",
                    "parent_ts": None,
                    "checkpoint_payload": _sample_checkpoint("checkpoint-001"),
                    "metadata_payload": {"source": "input", "step": -1},
                },
            ]
        )
        saver = PostgresDurableCheckpointer(
            _FakePool(conn),
            worker_id="worker-123",
            tenant_id="default",
        )

        results = [
            item
            async for item in saver.alist(
                _sample_config(checkpoint_id=None),
                filter={"source": "loop"},
                before={"configurable": {"checkpoint_id": "checkpoint-003"}},
                limit=1,
            )
        ]

        assert [item.config["configurable"]["checkpoint_id"] for item in results] == [
            "checkpoint-002",
            "checkpoint-001",
        ]
        query = next(call[1] for call in conn.calls if call[0] == "fetch")
        assert "metadata_payload @>" in query
        assert "checkpoint_id <" in query
        assert "LIMIT" in query


def saver_typed(value: dict[str, Any]) -> tuple[str, bytes]:
    return PostgresDurableCheckpointer(
        _FakePool(_FakeConnection()),
        worker_id="worker-123",
        tenant_id="default",
    ).serde.dumps_typed(value)
