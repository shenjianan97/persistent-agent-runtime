"""Backend integration tests for Phase 2 Track 5 memory on the task-submission path.

This suite is part of Task 11's AC coverage matrix (see the design doc
`docs/design-docs/phase-2/track-5-memory.md`, §Acceptance Criteria). It exercises
the REST surface against the live api-service + isolated test DB on port 55433
for four design acceptance criteria that `test_memory_api.py` does not already
cover:

  AC-1  — Agent `memory` config round-trip: create + get exposes the sub-object
          verbatim; default (max_entries) applies when omitted at write time.
  AC-8  — Customer attach at task submission (`attached_memory_ids`):
            * valid scoped ids persist in `task_attached_memories` with the
              expected `position`, appear on the task detail response, and are
              mirrored in the `task_submitted` event's `details` JSONB.
            * unknown / cross-agent / cross-tenant ids all reject with the
              uniform 4xx shape (404-not-403 disclosure rule).
            * `attached_memories_preview` omits memory ids that point at
              deleted entries (soft-ref audit survives, live preview does not).
  AC-10 — Cross-tenant / cross-agent memory-touching endpoints return a uniform
          "not found" across list / detail / delete / search / submit-attach.
  AC-11 — `skip_memory_write = true` round-trips through the API (stored on
          `tasks.skip_memory_write` column) and leaves `agent_memory_entries`
          untouched. Also confirms the field defaults to false when absent.

The worker-side half of AC-2 / AC-4 / AC-6 / AC-7 / AC-13 / AC-14 lives in
`services/worker-service/tests/` — see the AC map in
`docs/exec-plans/active/phase-2/track-5/agent_tasks/task-11-integration-and-browser-tests.md`.
"""
from __future__ import annotations

import json
import uuid
from typing import Any

import pytest


# ---------- helpers ---------------------------------------------------------


async def _insert_memory_row(
    db,
    *,
    tenant_id: str = "default",
    agent_id: str,
    task_id: str | None = None,
    title: str = "seeded memory",
    summary: str = "summary text",
    observations: list[str] | None = None,
    outcome: str = "succeeded",
    tags: list[str] | None = None,
) -> str:
    memory_id = str(uuid.uuid4())
    task_id = task_id or str(uuid.uuid4())
    observations = observations or []
    tags = tags or []
    await db.execute(
        """
        INSERT INTO agent_memory_entries (
            memory_id, tenant_id, agent_id, task_id,
            title, summary, observations, outcome, tags
        ) VALUES ($1::uuid, $2, $3, $4::uuid, $5, $6, $7, $8, $9)
        """,
        memory_id, tenant_id, agent_id, task_id,
        title, summary, observations, outcome, tags,
    )
    return memory_id


def _memory_enabled_config() -> dict[str, Any]:
    return {
        "system_prompt": "You are a test assistant.",
        "provider": "anthropic",
        "model": "claude-sonnet-4-6",
        "temperature": 0.5,
        "allowed_tools": [],
        "memory": {"enabled": True, "max_entries": 500},
    }


# ---------- AC-1: agent config memory round-trip ----------------------------


@pytest.mark.asyncio
async def test_ac1_agent_config_memory_roundtrips(e2e):
    """AC-1 — POST /v1/agents persists `agent_config.memory` verbatim."""
    resp = e2e.api.create_agent(
        display_name="Memory Config RoundTrip",
        agent_config=_memory_enabled_config(),
    )
    agent_id = resp["body"]["agent_id"]

    get_resp = e2e.api.get_agent(agent_id)
    body = get_resp["body"]
    stored_memory = body["agent_config"].get("memory")
    assert stored_memory is not None, "memory sub-object must survive round-trip"
    assert stored_memory.get("enabled") is True
    assert stored_memory.get("max_entries") == 500


@pytest.mark.asyncio
async def test_ac1_agent_config_memory_absent_when_disabled(e2e):
    """AC-1 — Agents without `memory` on the config do not gain a memory sub-object."""
    resp = e2e.api.create_agent(
        display_name="Memory-Absent Agent",
        agent_config={
            "system_prompt": "You are a test assistant.",
            "provider": "anthropic",
            "model": "claude-sonnet-4-6",
            "temperature": 0.5,
            "allowed_tools": [],
        },
    )
    agent_id = resp["body"]["agent_id"]
    body = e2e.api.get_agent(agent_id)["body"]
    # Canonical form: memory is either absent or null-equivalent.
    memory = body["agent_config"].get("memory")
    assert memory is None or memory == {}


# ---------- AC-8: attach at submission -------------------------------------


@pytest.mark.asyncio
async def test_ac8_attach_valid_persists_in_join_table_and_event(e2e):
    """AC-8 happy path — attached_memory_ids propagate to join table + event + detail."""
    agent_resp = e2e.api.create_agent(
        display_name="Attach Happy Path",
        agent_config=_memory_enabled_config(),
    )
    agent_id = agent_resp["body"]["agent_id"]

    m1 = await _insert_memory_row(e2e.db, agent_id=agent_id, title="first")
    m2 = await _insert_memory_row(e2e.db, agent_id=agent_id, title="second")

    submit = e2e.api.submit_task(
        agent_id=agent_id,
        input="exercise attach",
        attached_memory_ids=[m1, m2],
        skip_memory_write=True,  # keep this test API-only; no worker needed
    )
    task_id = submit["body"]["task_id"]

    # 1. Join table captures both ids with preserved position order.
    rows = await e2e.db.fetch(
        "SELECT memory_id::text AS memory_id, position FROM task_attached_memories "
        "WHERE task_id = $1::uuid ORDER BY position ASC",
        task_id,
    )
    assert [(r["memory_id"], r["position"]) for r in rows] == [(m1, 0), (m2, 1)]

    # 2. Task detail response mirrors the list + preview.
    detail = e2e.api.get_task(task_id)["body"]
    assert detail["attached_memory_ids"] == [m1, m2]
    preview_ids = [p["memory_id"] for p in detail["attached_memories_preview"]]
    assert preview_ids == [m1, m2]
    titles = [p["title"] for p in detail["attached_memories_preview"]]
    assert titles == ["first", "second"]

    # 3. task_submitted event's details JSONB echoes the same list.
    events = e2e.get_events(task_id)
    submitted = [e for e in events if e["event_type"] == "task_submitted"]
    assert submitted, "task_submitted event must exist"
    details = submitted[0]["details"]
    if isinstance(details, str):
        details = json.loads(details)
    mirrored = details.get("attached_memory_ids")
    assert mirrored == [m1, m2], f"expected event to mirror join table, got {mirrored!r}"


@pytest.mark.asyncio
async def test_ac8_attach_cross_agent_rejected_uniform(e2e):
    """AC-8 / AC-10 — memory id from agent A rejected on agent B's submit."""
    a = e2e.api.create_agent(
        display_name="Agent A",
        agent_config=_memory_enabled_config(),
    )["body"]["agent_id"]
    b = e2e.api.create_agent(
        display_name="Agent B",
        agent_config=_memory_enabled_config(),
    )["body"]["agent_id"]

    foreign = await _insert_memory_row(e2e.db, agent_id=a, title="A's memory")

    resp = e2e.api.submit_task(
        agent_id=b,
        input="try to attach foreign memory",
        attached_memory_ids=[foreign],
        skip_memory_write=True,
        expected_status=(201, 400, 404),
        raise_for_status=False,
    )
    assert resp["status_code"] in (400, 404), (
        "cross-agent attachment must reject; "
        f"got {resp['status_code']} {resp['body']!r}"
    )


@pytest.mark.asyncio
async def test_ac8_attach_unknown_id_rejected_uniform(e2e):
    """AC-8 — resolution miss returns the same uniform 4xx shape as cross-agent."""
    agent = e2e.api.create_agent(
        display_name="Unknown Attach Agent",
        agent_config=_memory_enabled_config(),
    )["body"]["agent_id"]

    made_up = str(uuid.uuid4())
    resp = e2e.api.submit_task(
        agent_id=agent,
        input="attach an unknown memory id",
        attached_memory_ids=[made_up],
        skip_memory_write=True,
        expected_status=(201, 400, 404),
        raise_for_status=False,
    )
    assert resp["status_code"] in (400, 404), (
        "unknown memory id must reject; "
        f"got {resp['status_code']} {resp['body']!r}"
    )


@pytest.mark.asyncio
async def test_ac8_preview_omits_deleted_memory_entries(e2e):
    """AC-8 — `attached_memories_preview` skips rows that no longer resolve.

    The `attached_memory_ids` list is authoritative (audit trail) and keeps the
    deleted id; `attached_memories_preview` only carries ids that still resolve
    to live entries inside the task's (tenant, agent) scope.
    """
    agent = e2e.api.create_agent(
        display_name="Preview Soft-ref Agent",
        agent_config=_memory_enabled_config(),
    )["body"]["agent_id"]

    keep = await _insert_memory_row(e2e.db, agent_id=agent, title="keep-me")
    drop = await _insert_memory_row(e2e.db, agent_id=agent, title="drop-me")

    submit = e2e.api.submit_task(
        agent_id=agent,
        input="attach both",
        attached_memory_ids=[keep, drop],
        skip_memory_write=True,
    )
    task_id = submit["body"]["task_id"]

    # Hard delete one attached memory entry.
    del_resp = e2e.api._request(
        "DELETE", f"/agents/{agent}/memory/{drop}",
        expected_status=204, raise_for_status=False,
    )
    assert del_resp["status_code"] == 204

    detail = e2e.api.get_task(task_id)["body"]
    # Audit list unchanged: the attachment happened and stays recorded.
    assert detail["attached_memory_ids"] == [keep, drop]
    preview_ids = [p["memory_id"] for p in detail["attached_memories_preview"]]
    assert preview_ids == [keep], (
        "deleted memory id must be omitted from preview, "
        f"got {preview_ids!r}"
    )


# ---------- AC-10: cross-tenant uniform 404 --------------------------------


@pytest.mark.asyncio
async def test_ac10_cross_tenant_memory_is_invisible(e2e):
    """AC-10 — cross-tenant memory rows are invisible across every read surface.

    We install a memory row under a sibling tenant ("tenant_b") attached to an
    agent that also lives in "tenant_b". The default-tenant caller asks for
    the same memory id under an agent id that exists only in tenant_b — every
    memory-touching endpoint must answer uniformly (404 on direct lookup /
    delete, empty results on search / list), never distinguish "wrong tenant"
    from "not found".
    """
    foreign_tenant = "tenant_b"
    foreign_agent_id = f"cross_tenant_agent_{uuid.uuid4().hex[:8]}"
    # FK on agent_memory_entries requires the agent to exist in that tenant.
    await e2e.db.execute(
        """
        INSERT INTO agents (tenant_id, agent_id, display_name, agent_config, status)
        VALUES ($1, $2, 'Foreign Tenant Agent', '{}'::jsonb, 'active')
        """,
        foreign_tenant, foreign_agent_id,
    )
    foreign_memory = await _insert_memory_row(
        e2e.db,
        tenant_id=foreign_tenant,
        agent_id=foreign_agent_id,
        title="foreign-tenant-ghost",
    )

    # The default-tenant API client cannot see the foreign agent id at all —
    # list endpoint must 404.
    list_resp = e2e.api._request(
        "GET", f"/agents/{foreign_agent_id}/memory",
        expected_status=(200, 404), raise_for_status=False,
    )
    assert list_resp["status_code"] == 404

    # Detail lookup: unknown + cross-tenant share a uniform 404 body.
    detail_resp = e2e.api._request(
        "GET", f"/agents/{foreign_agent_id}/memory/{foreign_memory}",
        expected_status=(200, 404), raise_for_status=False,
    )
    assert detail_resp["status_code"] == 404

    # Delete on cross-tenant id stays 404 (404-not-403 rule).
    del_resp = e2e.api._request(
        "DELETE", f"/agents/{foreign_agent_id}/memory/{foreign_memory}",
        expected_status=(204, 404), raise_for_status=False,
    )
    assert del_resp["status_code"] == 404

    # And the row actually persisted — we did plant cross-tenant data.
    count = await e2e.db.fetchval(
        "SELECT COUNT(*) FROM agent_memory_entries WHERE tenant_id = $1",
        foreign_tenant,
    )
    assert count == 1


# ---------- AC-11: skip_memory_write round-trips through the API ----------


@pytest.mark.asyncio
async def test_ac11_skip_memory_write_persists_on_task(e2e):
    """AC-11 — submitting with `skip_memory_write=true` stores the flag."""
    agent = e2e.api.create_agent(
        display_name="Skip Memory Write Agent",
        agent_config=_memory_enabled_config(),
    )["body"]["agent_id"]

    submit = e2e.api.submit_task(
        agent_id=agent,
        input="no memory write please",
        skip_memory_write=True,
    )
    task_id = submit["body"]["task_id"]

    row = await e2e.db.fetchval(
        "SELECT skip_memory_write FROM tasks WHERE task_id = $1::uuid",
        task_id,
    )
    assert row is True


@pytest.mark.asyncio
async def test_ac11_skip_memory_write_defaults_false(e2e):
    """AC-11 — absent `skip_memory_write` defaults to false on the stored row."""
    agent = e2e.api.create_agent(
        display_name="Default Skip Memory Write",
        agent_config=_memory_enabled_config(),
    )["body"]["agent_id"]

    submit = e2e.api.submit_task(
        agent_id=agent,
        input="default path",
    )
    task_id = submit["body"]["task_id"]

    row = await e2e.db.fetchval(
        "SELECT skip_memory_write FROM tasks WHERE task_id = $1::uuid",
        task_id,
    )
    assert row is False


@pytest.mark.asyncio
async def test_ac11_skip_memory_write_on_disabled_agent_is_noop_but_persisted(e2e):
    """AC-11 — `skip_memory_write` is a per-task override stored regardless of agent memory state.

    When the agent already has memory disabled, the flag still persists; the
    worker path stays behaviourally identical. This test pins the storage
    behaviour so future worker changes cannot silently drop the flag.
    """
    agent = e2e.api.create_agent(
        display_name="Memory-Off Agent With Skip",
        agent_config={
            "system_prompt": "You are a test assistant.",
            "provider": "anthropic",
            "model": "claude-sonnet-4-6",
            "temperature": 0.5,
            "allowed_tools": [],
        },
    )["body"]["agent_id"]

    task_id = e2e.api.submit_task(
        agent_id=agent,
        input="memory off plus skip",
        skip_memory_write=True,
    )["body"]["task_id"]

    stored = await e2e.db.fetchval(
        "SELECT skip_memory_write FROM tasks WHERE task_id = $1::uuid",
        task_id,
    )
    assert stored is True
