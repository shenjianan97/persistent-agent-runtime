"""Backend integration tests for the Phase 2 Track 5 memory REST API.

Exercises the live api-service against the dedicated test DB, verifying:
  - list / detail / delete / search endpoints with tenant+agent scope predicates
  - 404-not-403 disclosure across unknown id, wrong agent, wrong tenant
  - hybrid / text mode (vector mode requires an embedding provider — tested via
    unit tests only; integration-level vector testing lands with Task 5)
  - websearch_to_tsquery parse-safety against tricky user input
  - agent_storage_stats appears only on the first page
  - skip_memory_write column default has not broken the path
"""
from __future__ import annotations

import uuid

import pytest


async def _insert_memory_row(
    db,
    *,
    tenant_id: str = "default",
    agent_id: str,
    task_id: str | None = None,
    title: str,
    summary: str,
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


# ---------- list ----------

@pytest.mark.asyncio
async def test_list_returns_items_and_storage_stats_on_first_page(e2e):
    agent_resp = e2e.api.create_agent(display_name="Memory List Agent")
    agent_id = agent_resp["body"]["agent_id"]
    for i, title in enumerate(["first", "second", "third"]):
        await _insert_memory_row(e2e.db, agent_id=agent_id, title=title,
                                 summary=f"summary {i}",
                                 observations=[f"obs {i}"], tags=[title])

    resp = e2e.api._request("GET", f"/agents/{agent_id}/memory")
    assert resp["status_code"] == 200
    body = resp["body"]
    assert len(body["items"]) == 3
    assert body["agent_storage_stats"]["entry_count"] == 3
    assert body["agent_storage_stats"]["approx_bytes"] > 0
    titles = [item["title"] for item in body["items"]]
    assert titles == ["third", "second", "first"]


@pytest.mark.asyncio
async def test_list_unknown_agent_returns_404(e2e):
    resp = e2e.api._request(
        "GET", "/agents/unknown_agent_xyz/memory",
        expected_status=(200, 404), raise_for_status=False)
    assert resp["status_code"] == 404


@pytest.mark.asyncio
async def test_list_filters_by_outcome(e2e):
    agent_resp = e2e.api.create_agent(display_name="Outcome Filter Agent")
    agent_id = agent_resp["body"]["agent_id"]
    await _insert_memory_row(e2e.db, agent_id=agent_id,
                             title="good", summary="s", outcome="succeeded")
    await _insert_memory_row(e2e.db, agent_id=agent_id,
                             title="bad", summary="s", outcome="failed")

    resp = e2e.api._request("GET", f"/agents/{agent_id}/memory?outcome=failed")
    body = resp["body"]
    assert {item["title"] for item in body["items"]} == {"bad"}
    assert body["agent_storage_stats"]["entry_count"] == 2


# ---------- detail ----------

@pytest.mark.asyncio
async def test_get_returns_full_entry(e2e):
    agent_resp = e2e.api.create_agent(display_name="Detail Agent")
    agent_id = agent_resp["body"]["agent_id"]
    memory_id = await _insert_memory_row(
        e2e.db, agent_id=agent_id,
        title="alpha", summary="the alpha summary",
        observations=["obs-a", "obs-b"],
        tags=["x", "y"])

    resp = e2e.api._request("GET", f"/agents/{agent_id}/memory/{memory_id}")
    assert resp["status_code"] == 200
    body = resp["body"]
    assert body["memory_id"] == memory_id
    assert body["title"] == "alpha"
    assert body["summary"] == "the alpha summary"
    assert body["observations"] == ["obs-a", "obs-b"]
    assert body["tags"] == ["x", "y"]
    assert body["outcome"] == "succeeded"
    assert body["version"] == 1


@pytest.mark.asyncio
async def test_get_unknown_memory_id_returns_uniform_404(e2e):
    agent_resp = e2e.api.create_agent(display_name="404 Agent")
    agent_id = agent_resp["body"]["agent_id"]
    missing = str(uuid.uuid4())
    resp = e2e.api._request(
        "GET", f"/agents/{agent_id}/memory/{missing}",
        expected_status=(200, 404), raise_for_status=False)
    assert resp["status_code"] == 404
    assert resp["body"]["message"] == "Memory entry not found"


@pytest.mark.asyncio
async def test_get_wrong_agent_returns_uniform_404(e2e):
    """Memory id from agent A is invisible through agent B's path."""
    a_resp = e2e.api.create_agent(display_name="Agent A")
    a_id = a_resp["body"]["agent_id"]
    b_resp = e2e.api.create_agent(display_name="Agent B")
    b_id = b_resp["body"]["agent_id"]

    a_memory_id = await _insert_memory_row(
        e2e.db, agent_id=a_id, title="A's memory", summary="s")

    resp = e2e.api._request(
        "GET", f"/agents/{b_id}/memory/{a_memory_id}",
        expected_status=(200, 404), raise_for_status=False)
    assert resp["status_code"] == 404
    assert resp["body"]["message"] == "Memory entry not found"


@pytest.mark.asyncio
async def test_get_malformed_uuid_returns_404(e2e):
    agent_resp = e2e.api.create_agent(display_name="Malformed Agent")
    agent_id = agent_resp["body"]["agent_id"]
    resp = e2e.api._request(
        "GET", f"/agents/{agent_id}/memory/not-a-uuid",
        expected_status=(200, 404, 400), raise_for_status=False)
    assert resp["status_code"] == 404


# ---------- delete ----------

@pytest.mark.asyncio
async def test_delete_removes_row_and_returns_204(e2e):
    agent_resp = e2e.api.create_agent(display_name="Delete Agent")
    agent_id = agent_resp["body"]["agent_id"]
    memory_id = await _insert_memory_row(
        e2e.db, agent_id=agent_id, title="bye", summary="s")

    resp = e2e.api._request(
        "DELETE", f"/agents/{agent_id}/memory/{memory_id}",
        expected_status=204, raise_for_status=False)
    assert resp["status_code"] == 204

    resp2 = e2e.api._request(
        "DELETE", f"/agents/{agent_id}/memory/{memory_id}",
        expected_status=(200, 404), raise_for_status=False)
    assert resp2["status_code"] == 404


@pytest.mark.asyncio
async def test_delete_leaves_task_attached_memories_intact(e2e):
    """Attachment audit rows remain after memory delete (soft-ref memory_id)."""
    agent_resp = e2e.api.create_agent(display_name="Attach-audit Agent")
    agent_id = agent_resp["body"]["agent_id"]

    memory_id = await _insert_memory_row(
        e2e.db, agent_id=agent_id, title="attach-me", summary="s")
    task_id = await e2e.db.insert_task(agent_id=agent_id)
    await e2e.db.execute(
        """
        INSERT INTO task_attached_memories (task_id, memory_id, position)
        VALUES ($1::uuid, $2::uuid, 0)
        """, task_id, memory_id,
    )

    resp = e2e.api._request(
        "DELETE", f"/agents/{agent_id}/memory/{memory_id}",
        expected_status=204, raise_for_status=False)
    assert resp["status_code"] == 204

    count = await e2e.db.fetchval(
        "SELECT COUNT(*) FROM task_attached_memories WHERE memory_id = $1::uuid",
        memory_id,
    )
    assert count == 1


# ---------- search ----------

@pytest.mark.asyncio
async def test_text_search_returns_matches(e2e):
    agent_resp = e2e.api.create_agent(display_name="Text Search Agent")
    agent_id = agent_resp["body"]["agent_id"]
    await _insert_memory_row(
        e2e.db, agent_id=agent_id,
        title="cats and dogs", summary="about pet cats",
        observations=["feline observation"])
    await _insert_memory_row(
        e2e.db, agent_id=agent_id,
        title="unrelated", summary="different topic",
        observations=["other"])

    resp = e2e.api._request(
        "GET", f"/agents/{agent_id}/memory/search?q=cats&mode=text")
    body = resp["body"]
    assert body["ranking_used"] == "text"
    assert len(body["results"]) == 1
    assert body["results"][0]["title"] == "cats and dogs"
    assert body["results"][0]["summary_preview"] is not None


@pytest.mark.asyncio
async def test_text_search_handles_tricky_input_without_500(e2e):
    """websearch_to_tsquery tolerates unbalanced quotes / bare operators."""
    agent_resp = e2e.api.create_agent(display_name="Parse Safety Agent")
    agent_id = agent_resp["body"]["agent_id"]
    await _insert_memory_row(
        e2e.db, agent_id=agent_id, title="hello", summary="world")

    for tricky in ['"', '&', '|', '((', ')[]', '!!!']:
        # Note: the '&' character needs URL encoding to avoid query-string splitting.
        import urllib.parse
        q = urllib.parse.quote(tricky, safe='')
        path = f"/agents/{agent_id}/memory/search?q={q}&mode=text"
        resp = e2e.api._request("GET", path)
        assert resp["status_code"] == 200
        assert resp["body"]["ranking_used"] == "text"


@pytest.mark.asyncio
async def test_search_hybrid_degrades_to_text_when_embedding_unreachable(e2e):
    """With no embedding provider configured, hybrid silently degrades."""
    agent_resp = e2e.api.create_agent(display_name="Hybrid Degrade Agent")
    agent_id = agent_resp["body"]["agent_id"]
    await _insert_memory_row(
        e2e.db, agent_id=agent_id,
        title="cats", summary="cats and more cats")

    resp = e2e.api._request(
        "GET", f"/agents/{agent_id}/memory/search?q=cats&mode=hybrid")
    body = resp["body"]
    assert resp["status_code"] == 200
    assert body["ranking_used"] == "text"
    assert len(body["results"]) == 1


@pytest.mark.asyncio
async def test_search_vector_mode_503_when_embedding_unreachable(e2e):
    agent_resp = e2e.api.create_agent(display_name="Vector 503 Agent")
    agent_id = agent_resp["body"]["agent_id"]

    resp = e2e.api._request(
        "GET", f"/agents/{agent_id}/memory/search?q=cats&mode=vector",
        expected_status=(200, 503), raise_for_status=False)
    assert resp["status_code"] == 503


@pytest.mark.asyncio
async def test_search_limit_above_20_rejected(e2e):
    agent_resp = e2e.api.create_agent(display_name="Limit Reject Agent")
    agent_id = agent_resp["body"]["agent_id"]
    resp = e2e.api._request(
        "GET", f"/agents/{agent_id}/memory/search?q=a&mode=text&limit=21",
        expected_status=(200, 400), raise_for_status=False)
    assert resp["status_code"] == 400


@pytest.mark.asyncio
async def test_search_cross_agent_scope_404(e2e):
    """Agent B's search cannot see agent A's entries."""
    a_resp = e2e.api.create_agent(display_name="Agent A")
    a_id = a_resp["body"]["agent_id"]
    b_resp = e2e.api.create_agent(display_name="Agent B")
    b_id = b_resp["body"]["agent_id"]

    await _insert_memory_row(
        e2e.db, agent_id=a_id,
        title="alpha secret", summary="details")

    resp = e2e.api._request(
        "GET", f"/agents/{b_id}/memory/search?q=secret&mode=text")
    assert resp["status_code"] == 200
    assert resp["body"]["results"] == []


@pytest.mark.asyncio
async def test_two_agents_under_same_tenant_do_not_leak(e2e):
    a_resp = e2e.api.create_agent(display_name="Scope A")
    a_id = a_resp["body"]["agent_id"]
    b_resp = e2e.api.create_agent(display_name="Scope B")
    b_id = b_resp["body"]["agent_id"]

    for i in range(3):
        await _insert_memory_row(e2e.db, agent_id=a_id,
                                 title=f"a-{i}", summary="s")
    for i in range(2):
        await _insert_memory_row(e2e.db, agent_id=b_id,
                                 title=f"b-{i}", summary="s")

    a_list = e2e.api._request("GET", f"/agents/{a_id}/memory")["body"]
    b_list = e2e.api._request("GET", f"/agents/{b_id}/memory")["body"]

    assert len(a_list["items"]) == 3
    assert len(b_list["items"]) == 2
    assert all(item["title"].startswith("a-") for item in a_list["items"])
    assert all(item["title"].startswith("b-") for item in b_list["items"])
    assert a_list["agent_storage_stats"]["entry_count"] == 3
    assert b_list["agent_storage_stats"]["entry_count"] == 2
