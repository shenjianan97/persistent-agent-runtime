"""Integration tests for GET /v1/tasks/{taskId}/conversation.

Phase 2 Track 7 Task 13 — Slice B (API read path).

These tests verify the endpoint shape, pagination semantics, limit clamping,
and tenant isolation. They insert rows directly into
``task_conversation_log`` rather than exercising the worker dual-write path
(that lives in Slice A and has its own worker integration tests); isolating
the API here means Slice B's correctness doesn't depend on a green Slice A.

Depends on migration 0017_task_conversation_log.sql having been applied.
If the migration has not yet merged, these tests will fail at table creation
or INSERT time — that's the expected signal, not a bug in the endpoint.
"""

from __future__ import annotations

import json
import uuid

import pytest
import pytest_asyncio


# ---------------------------------------------------------------------------
# Migration precondition
#
# Slice A (Task 13) owns migration 0017_task_conversation_log.sql. Until that
# lands in the shared migrations directory, the table does not exist and every
# test here would hit a "relation does not exist" error at INSERT. To make
# this file merge-independent of Slice A, skip the whole module when the
# table is absent. Once Slice A lands, the fixture is a no-op.
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(autouse=True)
async def _require_conversation_log_table(e2e):
    exists = await e2e.db.fetchval(
        "SELECT to_regclass('public.task_conversation_log')"
    )
    if exists is None:
        pytest.skip(
            "task_conversation_log table not present — Slice A migration 0017 "
            "has not been applied to the test DB yet. This skip vanishes once "
            "Slice A merges and the migration runs."
        )


# ---------------------------------------------------------------------------
# Helpers — direct DB inserts into the conversation log.
#
# Mirrors the v1 schema contracted in the Task 13 spec (§Schema). Columns:
#   entry_id, tenant_id, task_id, sequence(IDENTITY), checkpoint_id,
#   idempotency_key, kind, role, content_version, content, content_size,
#   metadata, created_at.
# ---------------------------------------------------------------------------


async def _insert_log_entry(
    db,
    *,
    tenant_id: str,
    task_id: str,
    kind: str,
    role: str | None,
    content: dict,
    metadata: dict | None = None,
    checkpoint_id: str | None = None,
    idempotency_key: str | None = None,
    content_version: int = 1,
) -> None:
    """Append one row. Uses gen_random_uuid() for idempotency_key when
    the caller doesn't care about dedup — tests typically want each call to
    produce a distinct row."""
    key = idempotency_key or f"test:{uuid.uuid4()}"
    content_json = json.dumps(content)
    metadata_json = json.dumps(metadata or {})
    await db.execute(
        """
        INSERT INTO task_conversation_log (
            tenant_id, task_id, checkpoint_id, idempotency_key,
            kind, role, content_version, content, content_size, metadata
        ) VALUES (
            $1, $2::uuid, $3, $4, $5, $6, $7, $8::jsonb, $9, $10::jsonb
        )
        """,
        tenant_id,
        task_id,
        checkpoint_id,
        key,
        kind,
        role,
        content_version,
        content_json,
        len(content_json),
        metadata_json,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_conversation_empty_task_returns_200_and_empty_page(e2e):
    """Fresh task with no log entries returns empty list and next_sequence=null."""
    e2e.ensure_agent()
    task_id = e2e.submit_task(input="conversation log endpoint — empty")

    resp = e2e.api.get_task_conversation(task_id)
    assert resp["status_code"] == 200
    body = resp["body"]
    assert body["entries"] == []
    assert body["next_sequence"] is None


@pytest.mark.asyncio
async def test_get_conversation_returns_inserted_entries_in_sequence_order(e2e):
    """Entries appear ordered by monotone `sequence`. Response shape matches spec."""
    e2e.ensure_agent()
    task_id = e2e.submit_task(input="conversation log endpoint — ordering")

    # Insert a realistic mix of kinds, including HITL.
    await _insert_log_entry(
        e2e.db, tenant_id="default", task_id=task_id,
        kind="user_turn", role="user", content={"text": "Do the thing"},
    )
    await _insert_log_entry(
        e2e.db, tenant_id="default", task_id=task_id,
        kind="agent_turn", role="assistant",
        content={"text": "On it"}, metadata={"message_id": "ai_1", "finish_reason": "tool_use"},
    )
    await _insert_log_entry(
        e2e.db, tenant_id="default", task_id=task_id,
        kind="tool_call", role="assistant",
        content={"tool_name": "web_search", "args": {"query": "foo"}, "call_id": "call_1"},
        metadata={"message_id": "ai_1"},
    )
    await _insert_log_entry(
        e2e.db, tenant_id="default", task_id=task_id,
        kind="hitl_pause", role="system",
        content={"reason": "tool_requires_approval", "prompt_to_user": "approve?"},
        metadata={"checkpoint_id": "ckpt_1", "tool_name": "web_search"},
    )

    resp = e2e.api.get_task_conversation(task_id)
    assert resp["status_code"] == 200
    entries = resp["body"]["entries"]
    assert len(entries) == 4
    # Monotone sequence
    sequences = [e["sequence"] for e in entries]
    assert sequences == sorted(sequences)

    # First entry shape — assert all required fields per spec.
    first = entries[0]
    assert first["kind"] == "user_turn"
    assert first["role"] == "user"
    assert first["content_version"] == 1
    assert first["content"] == {"text": "Do the thing"}
    assert first["metadata"] == {}
    assert first["content_size"] > 0
    assert first["created_at"]  # RFC3339 timestamp string

    # HITL pause — specifically confirm opaque content round-trip
    hitl = next(e for e in entries if e["kind"] == "hitl_pause")
    assert hitl["content"]["reason"] == "tool_requires_approval"
    assert hitl["content"]["prompt_to_user"] == "approve?"


@pytest.mark.asyncio
async def test_get_conversation_pagination_next_sequence_signals_more(e2e):
    """`next_sequence` is set when the page is full, null when partial."""
    e2e.ensure_agent()
    task_id = e2e.submit_task(input="conversation log endpoint — pagination")

    # Insert 5 entries. Request limit=3 → expect next_sequence=<3rd sequence>
    for i in range(5):
        await _insert_log_entry(
            e2e.db, tenant_id="default", task_id=task_id,
            kind="user_turn", role="user", content={"text": f"turn {i}"},
        )

    page1 = e2e.api.get_task_conversation(task_id, limit=3)["body"]
    assert len(page1["entries"]) == 3
    assert page1["next_sequence"] is not None
    assert page1["next_sequence"] == page1["entries"][-1]["sequence"]

    page2 = e2e.api.get_task_conversation(
        task_id, after_sequence=page1["next_sequence"], limit=3,
    )["body"]
    assert len(page2["entries"]) == 2, "second page holds the remaining two entries"
    assert page2["next_sequence"] is None, "partial page signals end-of-stream"

    # Ensure no entry appears twice across the two pages.
    seen = {e["sequence"] for e in page1["entries"]}
    for entry in page2["entries"]:
        assert entry["sequence"] not in seen


@pytest.mark.asyncio
async def test_get_conversation_after_sequence_filter_skips_earlier_entries(e2e):
    """`after_sequence=N` returns only entries with sequence > N (exclusive)."""
    e2e.ensure_agent()
    task_id = e2e.submit_task(input="conversation log endpoint — after_sequence")

    for i in range(3):
        await _insert_log_entry(
            e2e.db, tenant_id="default", task_id=task_id,
            kind="user_turn", role="user", content={"text": f"turn {i}"},
        )

    all_entries = e2e.api.get_task_conversation(task_id)["body"]["entries"]
    assert len(all_entries) == 3
    first_seq = all_entries[0]["sequence"]

    filtered = e2e.api.get_task_conversation(
        task_id, after_sequence=first_seq,
    )["body"]["entries"]
    assert len(filtered) == 2
    assert all(e["sequence"] > first_seq for e in filtered)


@pytest.mark.asyncio
async def test_get_conversation_unknown_task_returns_404(e2e):
    """Nonexistent task_id → 404 (no enumeration oracle)."""
    resp = e2e.api.get_task_conversation(
        "00000000-0000-0000-0000-000000000000",
        raise_for_status=False,
    )
    assert resp["status_code"] == 404


@pytest.mark.asyncio
async def test_get_conversation_cross_tenant_returns_404(e2e):
    """Task owned by a different tenant returns 404 — never 403, never leaks.

    Tenant A (the API's default) MUST NOT observe tenant B's task, and MUST NOT
    receive a distinguishable error code. Any 4xx other than 404 would be a
    regression (403 would reveal the task exists; 500 would be a bug).
    """
    # Create a task that belongs to a different tenant. insert_task bypasses
    # the API so we can pin tenant_id ourselves.
    other_task_id = await e2e.db.insert_task(
        tenant_id="other_tenant_for_conversation_log",
        status="completed",
        agent_id="other_agent",
    )

    # Insert a log row scoped to that other tenant so we can verify NO entries
    # leak even though the table technically has a row for this task_id.
    await _insert_log_entry(
        e2e.db, tenant_id="other_tenant_for_conversation_log", task_id=other_task_id,
        kind="user_turn", role="user", content={"text": "tenant-b secret"},
    )

    resp = e2e.api.get_task_conversation(other_task_id, raise_for_status=False)
    assert resp["status_code"] == 404, (
        f"cross-tenant request must 404, got {resp['status_code']}: {resp['body']}"
    )


@pytest.mark.asyncio
async def test_get_conversation_limit_over_max_returns_400(e2e):
    """limit > 1000 is rejected with 400 (spec §API endpoint max limit)."""
    e2e.ensure_agent()
    task_id = e2e.submit_task(input="conversation log endpoint — limit clamp")

    resp = e2e.api.get_task_conversation(
        task_id, limit=5000, raise_for_status=False,
    )
    assert resp["status_code"] == 400


@pytest.mark.asyncio
async def test_get_conversation_limit_zero_returns_400(e2e):
    """limit=0 is rejected with 400."""
    e2e.ensure_agent()
    task_id = e2e.submit_task(input="conversation log endpoint — zero limit")

    resp = e2e.api.get_task_conversation(
        task_id, limit=0, raise_for_status=False,
    )
    assert resp["status_code"] == 400


@pytest.mark.asyncio
async def test_get_conversation_content_version_2_degrades_gracefully(e2e):
    """A content_version=2 row (forward-compat) is served with opaque content.

    Clients built against v1 render this as a debug-fold; the API contract
    is that the raw JsonNode round-trips regardless of version.
    """
    e2e.ensure_agent()
    task_id = e2e.submit_task(input="conversation log endpoint — v2 fwd-compat")

    await _insert_log_entry(
        e2e.db, tenant_id="default", task_id=task_id,
        kind="agent_turn", role="assistant",
        content={"text": "hello", "future_field": {"nested": True}},
        content_version=2,
    )

    entries = e2e.api.get_task_conversation(task_id)["body"]["entries"]
    assert len(entries) == 1
    entry = entries[0]
    assert entry["content_version"] == 2
    assert entry["content"]["future_field"] == {"nested": True}
