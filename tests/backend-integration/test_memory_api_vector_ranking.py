"""Backend integration tests for Phase 2 Track 5 hybrid RRF ranking.

Closes the AC-6 coverage gap left by ``test_memory_api.py``: that file
covers text mode and the 503 shape when the embedding provider is down,
but never seeds real 1536-d vectors nor asserts that vector-mode cosine
ranking and hybrid RRF fusion produce the expected order.

These tests drive a stubbed embedding provider (a fixed-port HTTP mock
set up in ``conftest.py``) so the real api-service exercises its full
``searchVector`` and ``searchHybrid`` SQL paths against the pgvector test
database. **No mock of the DB or RRF SQL.** Those are the code under test.
"""
from __future__ import annotations

import uuid

import pytest


# ------------------------------------------------------------------ helpers


# 1536-d basis vectors used throughout these tests. Keeping them normalized
# keeps cosine similarity = dot product, which makes the expected ordering
# easy to reason about.
_DIM = 1536
_SQRT_HALF = 0.7071067811865476  # 1 / sqrt(2)


def _basis(index: int) -> list[float]:
    """Unit vector e_index in R^1536."""
    v = [0.0] * _DIM
    v[index] = 1.0
    return v


def _mid(i: int, j: int) -> list[float]:
    """Unit vector at 45° between e_i and e_j (cosine = sqrt(1/2) to either)."""
    v = [0.0] * _DIM
    v[i] = _SQRT_HALF
    v[j] = _SQRT_HALF
    return v


def _vec_literal(vec: list[float]) -> str:
    """pgvector text-literal form, matching core/memory_repository.py."""
    return "[" + ",".join(f"{float(v):.7f}" for v in vec) + "]"


async def _insert_memory_row_with_vec(
    db,
    *,
    tenant_id: str = "default",
    agent_id: str,
    title: str,
    summary: str,
    content_vec: list[float] | None,
    observations: list[str] | None = None,
    outcome: str = "succeeded",
    tags: list[str] | None = None,
) -> str:
    """Insert one row with a hand-crafted ``content_vec`` (or NULL)."""
    memory_id = str(uuid.uuid4())
    task_id = str(uuid.uuid4())
    observations = observations or []
    tags = tags or []
    vec_literal = _vec_literal(content_vec) if content_vec is not None else None
    await db.execute(
        """
        INSERT INTO agent_memory_entries (
            memory_id, tenant_id, agent_id, task_id,
            title, summary, observations, outcome, tags,
            content_vec
        ) VALUES (
            $1::uuid, $2, $3, $4::uuid,
            $5, $6, $7::text[], $8, $9::text[],
            CASE WHEN $10::text IS NULL THEN NULL ELSE $10::text::vector END
        )
        """,
        memory_id, tenant_id, agent_id, task_id,
        title, summary, observations, outcome, tags,
        vec_literal,
    )
    return memory_id


# ---------------------------------------------------- AC-6: vector ranking

@pytest.mark.asyncio
async def test_search_vector_mode_returns_cosine_ranked_results(
    e2e, embedding_mock,
):
    """AC-6 (vector-mode): vector-only search orders rows by cosine similarity.

    Three rows are seeded with orthogonal basis vectors so the pairwise
    cosines to the stubbed query vector are a known near > mid > far
    ordering. This test would pass trivially in ``test_text_search_*`` so
    it has to go through the vector SQL path — and that path only runs
    when the embedding provider returns a 1536-d vector.
    """
    agent_resp = e2e.api.create_agent(display_name="Vector Rank Agent")
    agent_id = agent_resp["body"]["agent_id"]

    near_vec = _basis(0)          # cosine to query = 1.0
    mid_vec = _mid(0, 1)          # cosine to query ~ 0.707
    orthogonal_vec = _basis(1)    # cosine to query = 0.0

    near_id = await _insert_memory_row_with_vec(
        e2e.db, agent_id=agent_id, title="near", summary="near row",
        content_vec=near_vec,
    )
    mid_id = await _insert_memory_row_with_vec(
        e2e.db, agent_id=agent_id, title="mid", summary="mid row",
        content_vec=mid_vec,
    )
    orthogonal_id = await _insert_memory_row_with_vec(
        e2e.db, agent_id=agent_id, title="orth", summary="orthogonal row",
        content_vec=orthogonal_vec,
    )

    # The api-service will call the mock once; it returns our target query
    # vector (same as near_vec → cosine-1 to that row).
    embedding_mock.set_next_vector(_basis(0))

    resp = e2e.api._request(
        "GET",
        f"/agents/{agent_id}/memory/search?q=anything&mode=vector",
    )
    body = resp["body"]
    assert resp["status_code"] == 200
    assert body["ranking_used"] == "vector"
    ids = [item["memory_id"] for item in body["results"]]
    assert ids == [near_id, mid_id, orthogonal_id], (
        f"expected [near, mid, orthogonal] by cosine; got {ids}"
    )


# ---------------------------------------------------- AC-6: hybrid RRF

@pytest.mark.asyncio
async def test_search_hybrid_rrf_fuses_text_and_vector_rankings(
    e2e, embedding_mock,
):
    """AC-6 (hybrid): RRF fuses BM25 + cosine with k=60 and 4x candidate pool.

    Layout (for query ``q='cache'``, stubbed query vector = e_1):
      - Row A: title+summary dense with 'cache' (BM25 rank 1); ``content_vec``
        NULL so the vector branch excludes it (design: rows with NULL vec
        stay findable via BM25).
      - Row B: no occurrence of 'cache' (BM25 excluded); vec = e_1
        (exact match, vector rank 1 of the 2 rows with vectors).
      - Row C: one mention of 'cache' (BM25 rank 2); vec = (e_0+e_1)/sqrt(2)
        (cosine 0.707, vector rank 2).

    Expected RRF scores (k=60):
      - A: 1/(60+1) + 0           ≈ 0.016393
      - B: 0          + 1/(60+1)  ≈ 0.016393
      - C: 1/(60+2)   + 1/(60+2)  ≈ 0.032258
    Expected order: C, then {A, B} tiebroken by ``created_at DESC``.

    The insert order is B (oldest) → A → C (newest). Tiebreaker between
    A and B prefers the newer row, so the final order is [C, A, B].

    Note on the NULL-vec choice for A: without it, the candidate pool
    (``4 × limit`` per ranker) is larger than the row count so every row
    ends up in both pools. That makes A's RRF score
    ``1/(60+1) + 1/(60+3) ≈ 0.03227`` — a microscopic lead over C's
    ``2/62 ≈ 0.03226`` that would make the assertion brittle and defeat
    the intent of testing fusion of two disjoint ranker outputs. A real
    corpus with more than ``4 × 5`` vectored rows would drop A from the
    vector top-N naturally.
    """
    agent_resp = e2e.api.create_agent(display_name="Hybrid RRF Agent")
    agent_id = agent_resp["body"]["agent_id"]

    # Insert B first so it is the older of the two BM25-excluded-vs-top-vector
    # pair; A second so the created_at tiebreak produces a deterministic
    # A-before-B ordering.
    b_id = await _insert_memory_row_with_vec(
        e2e.db, agent_id=agent_id,
        title="unrelated topic",
        summary="nothing about the query term here",
        content_vec=_basis(1),  # exact query match
    )
    a_id = await _insert_memory_row_with_vec(
        e2e.db, agent_id=agent_id,
        title="cache cache cache",
        summary="cache is king cache rules cache wins cache everywhere",
        observations=["cache cache cache"],
        content_vec=None,  # excluded from vector branch
    )
    c_id = await _insert_memory_row_with_vec(
        e2e.db, agent_id=agent_id,
        title="random notes",
        summary="one mention of cache buried in here",
        content_vec=_mid(0, 1),  # 45° to query
    )

    embedding_mock.set_next_vector(_basis(1))  # targets row B in vec branch

    resp = e2e.api._request(
        "GET",
        f"/agents/{agent_id}/memory/search?q=cache&mode=hybrid",
    )
    body = resp["body"]
    assert resp["status_code"] == 200
    assert body["ranking_used"] == "hybrid"
    ids = [item["memory_id"] for item in body["results"]]
    assert ids == [c_id, a_id, b_id], (
        f"expected [C, A, B] per RRF(k=60); got {ids}. "
        f"(A={a_id} B={b_id} C={c_id})"
    )

    # Sanity: the ``score`` field is surfaced (rrf_score) and is larger for C.
    score_by_id = {item["memory_id"]: item["score"] for item in body["results"]}
    assert score_by_id[c_id] > score_by_id[a_id]
    assert score_by_id[c_id] > score_by_id[b_id]


# ---------------------------------------------------- AC-6: hybrid degrade

@pytest.mark.asyncio
async def test_search_hybrid_degrades_to_text_when_embedding_returns_error(
    e2e, embedding_mock,
):
    """AC-6 (hybrid degrade): provider 5xx on hybrid → silent BM25 fallback.

    Matches the design doc: ``mode=hybrid`` with an unreachable embedding
    provider returns 200 with ``ranking_used='text'`` rather than 503.
    """
    agent_resp = e2e.api.create_agent(display_name="Hybrid Degrade Agent")
    agent_id = agent_resp["body"]["agent_id"]

    top_id = await _insert_memory_row_with_vec(
        e2e.db, agent_id=agent_id,
        title="cache cache cache",
        summary="cache dominant row",
        content_vec=_basis(0),
    )
    other_id = await _insert_memory_row_with_vec(
        e2e.db, agent_id=agent_id,
        title="one cache mention",
        summary="only one cache token here",
        content_vec=_basis(1),
    )

    embedding_mock.set_next_error(503)

    resp = e2e.api._request(
        "GET",
        f"/agents/{agent_id}/memory/search?q=cache&mode=hybrid",
    )
    body = resp["body"]
    assert resp["status_code"] == 200
    assert body["ranking_used"] == "text"
    ids = [item["memory_id"] for item in body["results"]]
    # BM25 alone: repeated tokens in title+summary rank higher than a single
    # mention.
    assert ids == [top_id, other_id]
