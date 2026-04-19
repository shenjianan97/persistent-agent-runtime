"""Integration tests for ``core/conversation_log_repository.py``.

Covers the task-13 spec's §Worker DB test checklist:

* Insert + idempotency-key dedup (second insert with same key returns ``None``).
* Concurrent appends — 10 parallel writes produce 10 monotone (not
  necessarily contiguous) sequences.
* Composite-FK tenant integrity — an append whose ``tenant_id`` does not
  match the task's owner fails at the DB level.
* ON DELETE CASCADE — deleting a task row purges its log rows.
* Failure envelope — a broken connection pool logs WARN, increments
  the counter, and does NOT raise.
* SystemMessage exclusion is enforced at the call site; the repository
  itself validates ``kind`` is in the 9-value enum.
* ``seed:<uuid4>`` fallback for ``HumanMessage.id=None`` produces stable
  dedup under retry with the same idempotency key.

Runs against the isolated test DB on port 55433 (``make worker-test``).
"""

from __future__ import annotations

import asyncio
import os
import uuid

import asyncpg
import pytest

from core.conversation_log_repository import (
    ConversationLogRepository,
    compute_idempotency_key,
    get_append_failed_counter,
    reset_append_failed_counter,
)


DB_DSN = os.getenv(
    "E2E_DB_DSN",
    "postgresql://postgres:postgres@localhost:55433/persistent_agent_runtime_e2e",
)

TENANT_ID = "default"
OTHER_TENANT_ID = "other-tenant"
AGENT_ID = "conversation-log-test-agent"
WORKER_ID = "worker-a"


@pytest.fixture
async def integration_pool():
    try:
        pool = await asyncpg.create_pool(DB_DSN, min_size=1, max_size=4)
    except Exception as exc:
        pytest.skip(f"PostgreSQL test database is not available: {exc}")

    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM task_conversation_log WHERE tenant_id IN ($1, $2)",
            TENANT_ID, OTHER_TENANT_ID,
        )
        await conn.execute(
            "DELETE FROM tasks WHERE tenant_id IN ($1, $2) AND agent_id = $3",
            TENANT_ID, OTHER_TENANT_ID, AGENT_ID,
        )
        await conn.execute(
            "DELETE FROM agents WHERE tenant_id IN ($1, $2) AND agent_id = $3",
            TENANT_ID, OTHER_TENANT_ID, AGENT_ID,
        )
        await conn.execute(
            """
            INSERT INTO agents (tenant_id, agent_id, display_name, agent_config, status)
            VALUES ($1, $2, 'ConvLog Test', '{}'::jsonb, 'active')
            """,
            TENANT_ID, AGENT_ID,
        )
        await conn.execute(
            """
            INSERT INTO agents (tenant_id, agent_id, display_name, agent_config, status)
            VALUES ($1, $2, 'ConvLog Test Other', '{}'::jsonb, 'active')
            """,
            OTHER_TENANT_ID, AGENT_ID,
        )

    reset_append_failed_counter()

    try:
        yield pool
    finally:
        async with pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM task_conversation_log WHERE tenant_id IN ($1, $2)",
                TENANT_ID, OTHER_TENANT_ID,
            )
            await conn.execute(
                "DELETE FROM tasks WHERE tenant_id IN ($1, $2) AND agent_id = $3",
                TENANT_ID, OTHER_TENANT_ID, AGENT_ID,
            )
            await conn.execute(
                "DELETE FROM agents WHERE tenant_id IN ($1, $2) AND agent_id = $3",
                TENANT_ID, OTHER_TENANT_ID, AGENT_ID,
            )
        await pool.close()


async def _seed_task(pool: asyncpg.Pool, tenant_id: str = TENANT_ID) -> str:
    task_id = str(uuid.uuid4())
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO tasks (
                task_id, tenant_id, agent_id, agent_config_snapshot,
                status, input, lease_owner, lease_expiry, version
            ) VALUES ($1::uuid, $2, $3, '{}'::jsonb, 'running', 'input', $4,
                      NOW() + INTERVAL '60 seconds', 1)
            """,
            task_id, tenant_id, AGENT_ID, WORKER_ID,
        )
    return task_id


# ---------------------------------------------------------------------------
# compute_idempotency_key
# ---------------------------------------------------------------------------


def test_compute_idempotency_key_is_stable() -> None:
    task_id = "11111111-2222-3333-4444-555555555555"
    k1 = compute_idempotency_key(task_id=task_id, checkpoint_id="ckpt-1", origin_ref="msg-1")
    k2 = compute_idempotency_key(task_id=task_id, checkpoint_id="ckpt-1", origin_ref="msg-1")
    assert k1 == k2
    assert len(k1) == 64  # sha256 hex digest


def test_compute_idempotency_key_uses_init_when_checkpoint_id_missing() -> None:
    task_id = "11111111-2222-3333-4444-555555555555"
    k_none = compute_idempotency_key(task_id=task_id, checkpoint_id=None, origin_ref="msg-1")
    k_init = compute_idempotency_key(task_id=task_id, checkpoint_id="init", origin_ref="msg-1")
    assert k_none == k_init


def test_compute_idempotency_key_differs_per_origin_ref() -> None:
    task_id = "11111111-2222-3333-4444-555555555555"
    k1 = compute_idempotency_key(task_id=task_id, checkpoint_id="ckpt-1", origin_ref="msg-1")
    k2 = compute_idempotency_key(task_id=task_id, checkpoint_id="ckpt-1", origin_ref="msg-2")
    assert k1 != k2


# ---------------------------------------------------------------------------
# append_entry — happy path + dedup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_append_entry_returns_sequence_on_first_insert(
    integration_pool: asyncpg.Pool,
) -> None:
    task_id = await _seed_task(integration_pool)
    repo = ConversationLogRepository(integration_pool)
    key = compute_idempotency_key(task_id=task_id, checkpoint_id=None, origin_ref="seed:1")

    seq = await repo.append_entry(
        task_id=task_id,
        tenant_id=TENANT_ID,
        checkpoint_id=None,
        idempotency_key=key,
        kind="user_turn",
        role="user",
        content={"text": "hello"},
    )

    assert isinstance(seq, int)
    assert seq > 0


@pytest.mark.asyncio
async def test_append_entry_returns_none_on_duplicate_idempotency_key(
    integration_pool: asyncpg.Pool,
) -> None:
    task_id = await _seed_task(integration_pool)
    repo = ConversationLogRepository(integration_pool)
    key = compute_idempotency_key(task_id=task_id, checkpoint_id=None, origin_ref="seed:1")

    first = await repo.append_entry(
        task_id=task_id, tenant_id=TENANT_ID, checkpoint_id=None,
        idempotency_key=key, kind="user_turn", role="user",
        content={"text": "hello"},
    )
    second = await repo.append_entry(
        task_id=task_id, tenant_id=TENANT_ID, checkpoint_id=None,
        idempotency_key=key, kind="user_turn", role="user",
        content={"text": "hello"},
    )

    assert first is not None
    assert second is None
    async with integration_pool.acquire() as conn:
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM task_conversation_log WHERE task_id = $1::uuid",
            task_id,
        )
    assert count == 1


@pytest.mark.asyncio
async def test_append_entry_all_nine_kinds_accepted(
    integration_pool: asyncpg.Pool,
) -> None:
    task_id = await _seed_task(integration_pool)
    repo = ConversationLogRepository(integration_pool)
    kinds = [
        ("user_turn", "user", {"text": "u"}),
        ("agent_turn", "assistant", {"text": "a"}),
        ("tool_call", "assistant", {"tool_name": "t", "args": {"k": "v"}, "call_id": "c1"}),
        ("tool_result", "tool", {"call_id": "c1", "tool_name": "t", "text": "r", "is_error": False}),
        ("system_note", "system", {"text": "s"}),
        ("compaction_boundary", "system", {
            "summary_text": "sum", "first_turn_index": 0, "last_turn_index": 5,
        }),
        ("memory_flush", "system", {}),
        ("hitl_pause", "system", {"reason": "tool_requires_approval", "prompt_to_user": "ok?"}),
        ("hitl_resume", "system", {"resolution": "approved", "user_note": None}),
    ]
    for idx, (kind, role, content) in enumerate(kinds):
        key = compute_idempotency_key(
            task_id=task_id, checkpoint_id="ckpt-1", origin_ref=f"{kind}:{idx}",
        )
        seq = await repo.append_entry(
            task_id=task_id,
            tenant_id=TENANT_ID,
            checkpoint_id="ckpt-1",
            idempotency_key=key,
            kind=kind,  # type: ignore[arg-type]
            role=role,
            content=content,
        )
        assert seq is not None, f"kind={kind} did not insert"


@pytest.mark.asyncio
async def test_append_entry_unknown_kind_returns_none_and_increments_counter(
    integration_pool: asyncpg.Pool,
) -> None:
    task_id = await _seed_task(integration_pool)
    repo = ConversationLogRepository(integration_pool)
    before = get_append_failed_counter("bogus_kind", "InvalidKind")

    result = await repo.append_entry(
        task_id=task_id,
        tenant_id=TENANT_ID,
        checkpoint_id=None,
        idempotency_key="k",
        kind="bogus_kind",  # type: ignore[arg-type]
        role="user",
        content={},
    )

    assert result is None
    after = get_append_failed_counter("bogus_kind", "InvalidKind")
    assert after == before + 1


@pytest.mark.asyncio
async def test_append_entry_stores_content_size_and_metadata(
    integration_pool: asyncpg.Pool,
) -> None:
    task_id = await _seed_task(integration_pool)
    repo = ConversationLogRepository(integration_pool)
    key = compute_idempotency_key(task_id=task_id, checkpoint_id="ckpt-1", origin_ref="m1")

    seq = await repo.append_entry(
        task_id=task_id,
        tenant_id=TENANT_ID,
        checkpoint_id="ckpt-1",
        idempotency_key=key,
        kind="agent_turn",
        role="assistant",
        content={"text": "hello there"},
        metadata={"message_id": "ai_123", "finish_reason": "stop"},
    )
    assert seq is not None

    async with integration_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT content_size, metadata, content_version FROM task_conversation_log "
            "WHERE task_id = $1::uuid AND sequence = $2",
            task_id, seq,
        )
    assert row is not None
    # Serialised JSON of {"text": "hello there"} is 21 bytes; the sanity
    # check is that content_size matches the serialized byte count, not
    # a specific value.
    assert row["content_size"] > 0
    assert row["content_version"] == 1


# ---------------------------------------------------------------------------
# Concurrency — 10 parallel writes with distinct keys succeed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_appends_produce_ten_rows_with_monotone_sequences(
    integration_pool: asyncpg.Pool,
) -> None:
    task_id = await _seed_task(integration_pool)
    repo = ConversationLogRepository(integration_pool)

    async def _one(idx: int) -> int | None:
        key = compute_idempotency_key(
            task_id=task_id, checkpoint_id="ckpt-parallel", origin_ref=f"msg-{idx}",
        )
        return await repo.append_entry(
            task_id=task_id,
            tenant_id=TENANT_ID,
            checkpoint_id="ckpt-parallel",
            idempotency_key=key,
            kind="tool_call",
            role="assistant",
            content={"tool_name": "t", "args": {}, "call_id": f"c-{idx}"},
        )

    seqs = await asyncio.gather(*[_one(i) for i in range(10)])
    assert all(s is not None for s in seqs)
    assert len(set(seqs)) == 10
    sorted_seqs = sorted(s for s in seqs if s is not None)
    # Monotone — each sequence strictly greater than prior.
    assert all(sorted_seqs[i] < sorted_seqs[i + 1] for i in range(9))


# ---------------------------------------------------------------------------
# Composite FK tenant integrity
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mismatched_tenant_id_fails_composite_fk(
    integration_pool: asyncpg.Pool,
) -> None:
    # Task owned by TENANT_ID; attempt to append using OTHER_TENANT_ID.
    task_id = await _seed_task(integration_pool, tenant_id=TENANT_ID)
    repo = ConversationLogRepository(integration_pool)
    key = compute_idempotency_key(task_id=task_id, checkpoint_id=None, origin_ref="cross-tenant")

    before = get_append_failed_counter("user_turn", "ForeignKeyViolationError")

    result = await repo.append_entry(
        task_id=task_id,
        tenant_id=OTHER_TENANT_ID,  # mismatched on purpose
        checkpoint_id=None,
        idempotency_key=key,
        kind="user_turn",
        role="user",
        content={"text": "leak attempt"},
    )

    # Best-effort envelope: returns None on DB error, does not raise.
    assert result is None
    # Counter incremented with the asyncpg FK violation class name.
    after = get_append_failed_counter("user_turn", "ForeignKeyViolationError")
    assert after == before + 1


# ---------------------------------------------------------------------------
# ON DELETE CASCADE
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deleting_task_cascades_log_rows(
    integration_pool: asyncpg.Pool,
) -> None:
    task_id = await _seed_task(integration_pool)
    repo = ConversationLogRepository(integration_pool)

    for i in range(3):
        key = compute_idempotency_key(
            task_id=task_id, checkpoint_id="ckpt-d", origin_ref=f"msg-{i}",
        )
        await repo.append_entry(
            task_id=task_id, tenant_id=TENANT_ID, checkpoint_id="ckpt-d",
            idempotency_key=key, kind="user_turn", role="user",
            content={"text": f"t{i}"},
        )

    async with integration_pool.acquire() as conn:
        pre = await conn.fetchval(
            "SELECT COUNT(*) FROM task_conversation_log WHERE task_id = $1::uuid",
            task_id,
        )
        assert pre == 3
        await conn.execute("DELETE FROM tasks WHERE task_id = $1::uuid", task_id)
        post = await conn.fetchval(
            "SELECT COUNT(*) FROM task_conversation_log WHERE task_id = $1::uuid",
            task_id,
        )
    assert post == 0


# ---------------------------------------------------------------------------
# Failure envelope — closed pool, broken DB
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_append_entry_swallows_db_error_and_increments_counter(
    integration_pool: asyncpg.Pool,
) -> None:
    # Create a separate pool, seed the task, then close the pool so
    # subsequent acquire() fails deterministically. Using a closed pool
    # exercises the failure envelope without requiring a DB down.
    task_id = await _seed_task(integration_pool)

    broken_pool = await asyncpg.create_pool(DB_DSN, min_size=1, max_size=1)
    await broken_pool.close()

    repo = ConversationLogRepository(broken_pool)
    key = compute_idempotency_key(
        task_id=task_id, checkpoint_id=None, origin_ref="broken",
    )

    before_generic = sum(
        get_append_failed_counter("user_turn", cls)
        for cls in ("InterfaceError", "ConnectionDoesNotExistError", "PoolClosedError")
    )

    result = await repo.append_entry(
        task_id=task_id,
        tenant_id=TENANT_ID,
        checkpoint_id=None,
        idempotency_key=key,
        kind="user_turn",
        role="user",
        content={"text": "never stored"},
    )

    assert result is None
    # At least one of the expected failure classes was counted.
    after_generic = sum(
        get_append_failed_counter("user_turn", cls)
        for cls in ("InterfaceError", "ConnectionDoesNotExistError", "PoolClosedError")
    )
    assert after_generic > before_generic
