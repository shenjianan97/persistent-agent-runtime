"""Integration tests for the Track 7 Task 13 dual-write path.

These tests exercise the graph.py helpers that populate the user-facing
conversation log in parallel with the LangGraph checkpointer:

* ``_convlog_append_pre_llm_turns`` — HumanMessage / ToolMessage entries,
  SystemMessage exclusion, ``seed:<uuid4>`` fallback for id=None.
* ``_convlog_append_llm_response`` — agent_turn + one tool_call per
  ``response.tool_calls``.
* ``_convlog_append_compaction_events`` — Tier3FiredEvent →
  compaction_boundary; MemoryFlushFiredEvent → memory_flush;
  Tier1Applied / Tier15Applied are NOT mirrored.
* Idempotency key retry — same message instance reused across two
  super-step attempts produces exactly one row.

Runs against the isolated test DB on port 55433.
"""

from __future__ import annotations

import os
import uuid

import asyncpg
import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from core.conversation_log_repository import ConversationLogRepository
from executor.compaction.pre_model_hook import (
    MemoryFlushFiredEvent,
    Tier3FiredEvent,
)
from executor.graph import (
    _convlog_append_compaction_events,
    _convlog_append_llm_response,
    _convlog_append_pre_llm_turns,
    _convlog_origin_ref_for_message,
    _emit_compaction_task_events,
)


DB_DSN = os.getenv(
    "E2E_DB_DSN",
    "postgresql://postgres:postgres@localhost:55433/persistent_agent_runtime_e2e",
)

TENANT_ID = "default"
AGENT_ID = "convlog-integration-agent"
WORKER_ID = "worker-a"


@pytest.fixture
async def integration_pool():
    try:
        pool = await asyncpg.create_pool(DB_DSN, min_size=1, max_size=2)
    except Exception as exc:
        pytest.skip(f"PostgreSQL test database is not available: {exc}")

    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM task_conversation_log WHERE tenant_id = $1", TENANT_ID
        )
        await conn.execute(
            "DELETE FROM task_events WHERE tenant_id = $1 AND agent_id = $2",
            TENANT_ID, AGENT_ID,
        )
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
            VALUES ($1, $2, 'ConvLog Integration', '{}'::jsonb, 'active')
            """,
            TENANT_ID, AGENT_ID,
        )

    try:
        yield pool
    finally:
        async with pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM task_conversation_log WHERE tenant_id = $1", TENANT_ID
            )
            await conn.execute(
                "DELETE FROM task_events WHERE tenant_id = $1 AND agent_id = $2",
                TENANT_ID, AGENT_ID,
            )
            await conn.execute(
                "DELETE FROM tasks WHERE tenant_id = $1 AND agent_id = $2",
                TENANT_ID, AGENT_ID,
            )
            await conn.execute(
                "DELETE FROM agents WHERE tenant_id = $1 AND agent_id = $2",
                TENANT_ID, AGENT_ID,
            )
        await pool.close()


async def _seed_task(pool: asyncpg.Pool) -> str:
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
            task_id, TENANT_ID, AGENT_ID, WORKER_ID,
        )
    return task_id


async def _fetch_entries(pool: asyncpg.Pool, task_id: str) -> list[asyncpg.Record]:
    async with pool.acquire() as conn:
        return await conn.fetch(
            "SELECT sequence, kind, role, content, metadata "
            "FROM task_conversation_log WHERE task_id = $1::uuid ORDER BY sequence",
            task_id,
        )


# ---------------------------------------------------------------------------
# _convlog_origin_ref_for_message — seed:<uuid4> fallback
# ---------------------------------------------------------------------------


def test_origin_ref_uses_seed_for_humanmessage_with_no_id() -> None:
    msg = HumanMessage(content="hi")
    assert msg.id is None
    ref = _convlog_origin_ref_for_message(msg)
    assert ref.startswith("seed:")
    # Subsequent calls on the same msg instance return the same ref
    # (stored on msg.id).
    assert _convlog_origin_ref_for_message(msg) == ref


def test_origin_ref_preserves_existing_id() -> None:
    msg = AIMessage(content="x", id="ai_existing_123")
    assert _convlog_origin_ref_for_message(msg) == "ai_existing_123"


# ---------------------------------------------------------------------------
# Pre-LLM turns — HumanMessage, ToolMessage, SystemMessage exclusion
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pre_llm_turns_append_humanmessage_and_toolmessage(
    integration_pool: asyncpg.Pool,
) -> None:
    task_id = await _seed_task(integration_pool)
    repo = ConversationLogRepository(integration_pool)

    messages = [
        SystemMessage(content="you are helpful"),       # index 0 — must be excluded
        HumanMessage(content="run the tool please"),   # index 1
        AIMessage(
            content="",
            id="ai_1",
            tool_calls=[{"name": "ls", "args": {}, "id": "call_1"}],
        ),                                             # index 2 — AIMessage excluded here (written post-LLM)
        ToolMessage(content="file1\nfile2", tool_call_id="call_1", name="ls"),  # index 3
    ]

    await _convlog_append_pre_llm_turns(
        repo,
        task_id=task_id,
        tenant_id=TENANT_ID,
        checkpoint_id="ckpt-1",
        messages=messages,
        last_super_step_message_count=0,
    )

    rows = await _fetch_entries(integration_pool, task_id)
    kinds = [r["kind"] for r in rows]
    # SystemMessage excluded; AIMessage not handled here (post-LLM path).
    assert "system_note" not in kinds
    assert kinds == ["user_turn", "tool_result"]
    # Content snapshots
    assert rows[0]["content"] is not None
    import json as _json
    user_content = _json.loads(rows[0]["content"]) if isinstance(rows[0]["content"], str) else rows[0]["content"]
    assert user_content["text"] == "run the tool please"
    tool_content = _json.loads(rows[1]["content"]) if isinstance(rows[1]["content"], str) else rows[1]["content"]
    assert tool_content["call_id"] == "call_1"
    assert tool_content["tool_name"] == "ls"
    assert tool_content["text"] == "file1\nfile2"
    assert tool_content["is_error"] is False


@pytest.mark.asyncio
async def test_pre_llm_turns_respects_last_super_step_watermark(
    integration_pool: asyncpg.Pool,
) -> None:
    task_id = await _seed_task(integration_pool)
    repo = ConversationLogRepository(integration_pool)

    old_msg = HumanMessage(content="old", id="h_old")
    new_msg = HumanMessage(content="new", id="h_new")
    messages = [old_msg, new_msg]
    # Watermark = 1 → only `messages[1:]` is considered "new" this super-step
    await _convlog_append_pre_llm_turns(
        repo,
        task_id=task_id,
        tenant_id=TENANT_ID,
        checkpoint_id="ckpt-2",
        messages=messages,
        last_super_step_message_count=1,
    )
    rows = await _fetch_entries(integration_pool, task_id)
    assert len(rows) == 1
    import json as _json
    content = _json.loads(rows[0]["content"]) if isinstance(rows[0]["content"], str) else rows[0]["content"]
    assert content["text"] == "new"


@pytest.mark.asyncio
async def test_pre_llm_turns_retry_same_messages_dedups(
    integration_pool: asyncpg.Pool,
) -> None:
    """A super-step retry with the same message instances is a no-op."""
    task_id = await _seed_task(integration_pool)
    repo = ConversationLogRepository(integration_pool)

    hm = HumanMessage(content="hi")  # id=None — seed:uuid4() fallback
    messages = [hm]

    await _convlog_append_pre_llm_turns(
        repo,
        task_id=task_id,
        tenant_id=TENANT_ID,
        checkpoint_id="ckpt-3",
        messages=messages,
        last_super_step_message_count=0,
    )
    first_rows = await _fetch_entries(integration_pool, task_id)
    assert len(first_rows) == 1
    # The seed id is now persisted on the message object; a retry must
    # reuse the same idempotency key and therefore the same row.
    await _convlog_append_pre_llm_turns(
        repo,
        task_id=task_id,
        tenant_id=TENANT_ID,
        checkpoint_id="ckpt-3",
        messages=messages,
        last_super_step_message_count=0,
    )
    second_rows = await _fetch_entries(integration_pool, task_id)
    assert len(second_rows) == 1


# ---------------------------------------------------------------------------
# LLM response — agent_turn + tool_call per tool_calls entry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_llm_response_writes_agent_turn_and_tool_calls(
    integration_pool: asyncpg.Pool,
) -> None:
    task_id = await _seed_task(integration_pool)
    repo = ConversationLogRepository(integration_pool)

    response = AIMessage(
        content="I will list the dir.",
        id="ai_xyz",
        tool_calls=[
            {"name": "ls", "args": {"path": "/tmp"}, "id": "call_a"},
            {"name": "cat", "args": {"path": "/tmp/x"}, "id": "call_b"},
        ],
    )

    await _convlog_append_llm_response(
        repo,
        task_id=task_id,
        tenant_id=TENANT_ID,
        checkpoint_id="ckpt-4",
        response=response,
    )
    rows = await _fetch_entries(integration_pool, task_id)
    kinds = [r["kind"] for r in rows]
    assert kinds == ["agent_turn", "tool_call", "tool_call"]

    import json as _json
    agent_content = _json.loads(rows[0]["content"]) if isinstance(rows[0]["content"], str) else rows[0]["content"]
    assert agent_content["text"] == "I will list the dir."
    call_a = _json.loads(rows[1]["content"]) if isinstance(rows[1]["content"], str) else rows[1]["content"]
    assert call_a["tool_name"] == "ls"
    assert call_a["call_id"] == "call_a"
    assert call_a["args"] == {"path": "/tmp"}


@pytest.mark.asyncio
async def test_llm_response_handles_non_json_native_args(
    integration_pool: asyncpg.Pool,
) -> None:
    from pathlib import Path
    task_id = await _seed_task(integration_pool)
    repo = ConversationLogRepository(integration_pool)
    response = AIMessage(
        content="ok",
        id="ai_path",
        tool_calls=[
            {"name": "read", "args": {"path": Path("/tmp/x")}, "id": "call_p"},
        ],
    )

    await _convlog_append_llm_response(
        repo,
        task_id=task_id,
        tenant_id=TENANT_ID,
        checkpoint_id="ckpt-p",
        response=response,
    )
    rows = await _fetch_entries(integration_pool, task_id)
    import json as _json
    tc = _json.loads(rows[1]["content"]) if isinstance(rows[1]["content"], str) else rows[1]["content"]
    # Path coerced to str per json.dumps(default=str)
    assert tc["args"]["path"] == "/tmp/x"


# ---------------------------------------------------------------------------
# Compaction events — Tier3 visible, Tier1/1.5 invisible
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tier3_fired_emits_compaction_boundary(
    integration_pool: asyncpg.Pool,
) -> None:
    task_id = await _seed_task(integration_pool)
    repo = ConversationLogRepository(integration_pool)

    ev = Tier3FiredEvent(
        summarizer_model_id="claude-haiku-4-5",
        tokens_in=3500,
        tokens_out=220,
        new_summarized_through=12,
        task_id=task_id,
        tenant_id=TENANT_ID,
        agent_id=AGENT_ID,
    )

    await _convlog_append_compaction_events(
        repo,
        task_id=task_id,
        tenant_id=TENANT_ID,
        checkpoint_id="ckpt-t3",
        events=[ev],
        summarized_through_before=5,
        summary_before="",
        summary_after="Earlier: agent explored /tmp.",
    )

    rows = await _fetch_entries(integration_pool, task_id)
    assert len(rows) == 1
    assert rows[0]["kind"] == "compaction_boundary"
    import json as _json
    content = _json.loads(rows[0]["content"]) if isinstance(rows[0]["content"], str) else rows[0]["content"]
    metadata = _json.loads(rows[0]["metadata"]) if isinstance(rows[0]["metadata"], str) else rows[0]["metadata"]
    assert content["summary_text"] == "Earlier: agent explored /tmp."
    assert content["first_turn_index"] == 5
    assert content["last_turn_index"] == 12
    assert metadata["summarizer_model"] == "claude-haiku-4-5"
    assert metadata["turns_summarized"] == 7


@pytest.mark.asyncio
async def test_tier3_second_fire_logs_replaced_summary(
    integration_pool: asyncpg.Pool,
) -> None:
    """Second Tier 3 firing within same task: summary_text is the NEW summary.

    Track 7 Follow-up (Task 3) replaces ``summary`` each firing instead of
    appending, so the conversation-log entry simply records the replacement.
    """
    task_id = await _seed_task(integration_pool)
    repo = ConversationLogRepository(integration_pool)

    ev = Tier3FiredEvent(
        summarizer_model_id="claude-haiku-4-5",
        tokens_in=4100,
        tokens_out=300,
        new_summarized_through=25,
    )
    await _convlog_append_compaction_events(
        repo,
        task_id=task_id,
        tenant_id=TENANT_ID,
        checkpoint_id="ckpt-t3b",
        events=[ev],
        summarized_through_before=12,
        summary_before="Earlier: A.",
        summary_after="Rewritten: combined A + B.",
    )
    rows = await _fetch_entries(integration_pool, task_id)
    import json as _json
    content = _json.loads(rows[0]["content"]) if isinstance(rows[0]["content"], str) else rows[0]["content"]
    assert content["summary_text"] == "Rewritten: combined A + B."


@pytest.mark.asyncio
async def test_empty_events_list_produces_no_log_entries(
    integration_pool: asyncpg.Pool,
) -> None:
    """Track 7 Follow-up (Task 3): with no Tier3Fired / MemoryFlush events
    the compaction helper produces zero log entries. Tier 1 / Tier 1.5 event
    types were removed by the replace-and-rehydrate rewrite.
    """
    task_id = await _seed_task(integration_pool)
    repo = ConversationLogRepository(integration_pool)

    await _convlog_append_compaction_events(
        repo,
        task_id=task_id,
        tenant_id=TENANT_ID,
        checkpoint_id="ckpt-silent",
        events=[],
        summarized_through_before=0,
        summary_before="",
        summary_after="",
    )
    rows = await _fetch_entries(integration_pool, task_id)
    assert rows == []


@pytest.mark.asyncio
async def test_memory_flush_event_emits_memory_flush_entry(
    integration_pool: asyncpg.Pool,
) -> None:
    task_id = await _seed_task(integration_pool)
    repo = ConversationLogRepository(integration_pool)

    ev = MemoryFlushFiredEvent(
        fired_at_step=8,
        task_id=task_id,
        tenant_id=TENANT_ID,
        agent_id=AGENT_ID,
    )
    await _convlog_append_compaction_events(
        repo,
        task_id=task_id,
        tenant_id=TENANT_ID,
        checkpoint_id="ckpt-mf",
        events=[ev],
        summarized_through_before=0,
        summary_before="",
        summary_after="",
    )
    rows = await _fetch_entries(integration_pool, task_id)
    assert len(rows) == 1
    assert rows[0]["kind"] == "memory_flush"
    import json as _json
    metadata = _json.loads(rows[0]["metadata"]) if isinstance(rows[0]["metadata"], str) else rows[0]["metadata"]
    assert metadata["fired_at_step"] == 8


@pytest.mark.asyncio
async def test_tier3_fired_emits_task_compaction_event(
    integration_pool: asyncpg.Pool,
) -> None:
    """Tier 3 firings surface in the Execution History tab via task_events."""
    task_id = await _seed_task(integration_pool)

    ev = Tier3FiredEvent(
        summarizer_model_id="claude-haiku-4-5",
        tokens_in=91_426,
        tokens_out=1_445,
        new_summarized_through=42,
        task_id=task_id,
        tenant_id=TENANT_ID,
        agent_id=AGENT_ID,
    )
    await _emit_compaction_task_events(
        pool=integration_pool,
        task_id=task_id,
        tenant_id=TENANT_ID,
        agent_id=AGENT_ID,
        worker_id=WORKER_ID,
        events=[ev],
        summarized_through_before=10,
        summary_after="Earlier: agent explored files.",
    )

    async with integration_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT event_type, details::jsonb as details, worker_id "
            "FROM task_events WHERE task_id = $1::uuid ORDER BY created_at",
            task_id,
        )
    assert len(rows) == 1
    assert rows[0]["event_type"] == "task_compaction_fired"
    assert rows[0]["worker_id"] == WORKER_ID
    import json as _json
    details = _json.loads(rows[0]["details"]) if isinstance(rows[0]["details"], str) else rows[0]["details"]
    assert details["tier"] == 3
    assert details["tokens_in"] == 91_426
    assert details["tokens_out"] == 1_445
    assert details["turns_summarized"] == 32  # 42 - 10
    assert details["first_turn_index"] == 10
    assert details["last_turn_index"] == 42
    assert details["summarizer_model_id"] == "claude-haiku-4-5"
    assert details["summary_bytes"] == len("Earlier: agent explored files.".encode("utf-8"))


@pytest.mark.asyncio
async def test_tier3_fired_task_event_dedups_on_replay(
    integration_pool: asyncpg.Pool,
) -> None:
    """Replay of the same Tier-3 firing must not insert a second task_event row.

    Regression guard: without dedup, a crash between the task_event INSERT
    and the LangGraph checkpoint commit causes the replay to re-invoke
    ``_emit_compaction_task_events`` and double-mark the Execution History
    tab.
    """
    task_id = await _seed_task(integration_pool)

    ev = Tier3FiredEvent(
        summarizer_model_id="claude-haiku-4-5",
        tokens_in=1_000,
        tokens_out=200,
        new_summarized_through=42,
        task_id=task_id,
        tenant_id=TENANT_ID,
        agent_id=AGENT_ID,
    )
    for _ in range(3):
        await _emit_compaction_task_events(
            pool=integration_pool,
            task_id=task_id,
            tenant_id=TENANT_ID,
            agent_id=AGENT_ID,
            worker_id=WORKER_ID,
            events=[ev],
            summarized_through_before=10,
            summary_after="summary text",
        )

    async with integration_pool.acquire() as conn:
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM task_events "
            "WHERE task_id = $1::uuid AND event_type = 'task_compaction_fired'",
            task_id,
        )
    assert count == 1, (
        f"expected exactly one task_compaction_fired row per (task, watermark) "
        f"across replays; got {count}"
    )


@pytest.mark.asyncio
async def test_non_tier3_events_do_not_emit_task_event(
    integration_pool: asyncpg.Pool,
) -> None:
    """MemoryFlushFired alone — no task_compaction_fired row."""
    task_id = await _seed_task(integration_pool)

    await _emit_compaction_task_events(
        pool=integration_pool,
        task_id=task_id,
        tenant_id=TENANT_ID,
        agent_id=AGENT_ID,
        worker_id=WORKER_ID,
        events=[MemoryFlushFiredEvent(fired_at_step=1)],
        summarized_through_before=0,
        summary_after="",
    )
    async with integration_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT event_type FROM task_events WHERE task_id = $1::uuid",
            task_id,
        )
    assert rows == []


@pytest.mark.asyncio
async def test_memory_flush_event_dedups_within_task(
    integration_pool: asyncpg.Pool,
) -> None:
    """Idempotency key is `flush:{checkpoint_id}` — one row per checkpoint even if event is retried."""
    task_id = await _seed_task(integration_pool)
    repo = ConversationLogRepository(integration_pool)

    ev = MemoryFlushFiredEvent(fired_at_step=8)
    for _ in range(3):
        await _convlog_append_compaction_events(
            repo,
            task_id=task_id,
            tenant_id=TENANT_ID,
            checkpoint_id="ckpt-dup",
            events=[ev],
            summarized_through_before=0,
            summary_before="",
            summary_after="",
        )
    rows = await _fetch_entries(integration_pool, task_id)
    assert len(rows) == 1
