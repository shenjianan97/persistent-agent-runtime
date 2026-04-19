<!-- AGENT_TASK_START: task-3-memory-rest-api.md -->

# Task 3 — Memory REST API (List, Search, Detail, Delete)

## Agent Instructions

You are a software engineer implementing one module of a larger system. Your scope is strictly limited to this task.

**CRITICAL PRE-WORK:** Before beginning implementation, you MUST read:
1. `docs/design-docs/phase-2/track-5-memory.md` — sections "Read Path", "Search API", "API Surface", "Validation and Consistency Rules", and "Embeddings".
2. `services/api-service/.../controller/ToolServerController.java` + `service/ToolServerService.java` + `repository/ToolServerRepository.java` — the canonical Track-4 CRUD pattern to mirror.
3. `services/api-service/.../controller/AgentController.java` — for the `/v1/agents/{agent_id}/…` routing convention and tenant-scoping interceptor.
4. `services/api-service/.../service/observability/` — existing observability services (e.g., `TaskObservabilityService.java`, `CheckpointObservabilityService.java`). There is no shared generic "structured log helpers" class in this directory today — these are data/service helpers, not log utilities. Mirror the SLF4J structured-log STYLE these services use (key-value pairs, no PII) when writing the new `memory.search.*` log lines; if the log-line volume warrants it, add a thin `MemoryLogger.java` helper in the same package.
5. Relevant portions of the migration produced by Task 1 (columns, indexes, constraints on `agent_memory_entries`).
6. For the RRF behaviour and fallback semantics, read the design-doc "Search API" subsection end-to-end. The constants `k = 60` and `candidate_multiplier = 4` are platform-fixed in v1.

**CRITICAL POST-WORK:** After completing this task:
1. Run `make test`. Fix any regressions.
2. Run `make e2e-test` to exercise the endpoints against the test DB.
3. Update the status in `docs/exec-plans/active/phase-2/track-5/progress.md` to "Done".

## Context

Track 5 is a **customer-visible** memory store. This task delivers the HTTP surface customers and the Console use to browse, search, read, and delete entries. It is also the search backend the worker's `memory_search` tool (Task 7) delegates to, so the search endpoint's scope-binding, 404-shape, and RRF behaviour are all load-bearing invariants.

Four endpoints:

| Method | Path | Description |
|--------|------|-------------|
| GET | `/v1/agents/{agent_id}/memory` | Paginated list. Filters: `outcome`, `from`, `to`. First page includes `agent_storage_stats`. |
| GET | `/v1/agents/{agent_id}/memory/search` | Hybrid RRF search. Query params: `q` (required), `limit` (default 5, max 20), `mode` (`hybrid`\|`text`\|`vector`), `outcome`, `from`, `to`. |
| GET | `/v1/agents/{agent_id}/memory/{memory_id}` | Full entry. |
| DELETE | `/v1/agents/{agent_id}/memory/{memory_id}` | Hard delete. Matching `task_attached_memories` rows remain (see design doc). |

All four are scoped to the caller's `(tenant_id, agent_id)`. The 404-not-403 disclosure rule applies — any scope miss returns a uniform "not found" regardless of whether the id is unknown, from another tenant, or from another agent.

## Task-Specific Shared Contract

- **Scope predicate:** Every SQL query that reads `agent_memory_entries` MUST include `WHERE tenant_id = :bound AND agent_id = :bound`. The repository layer should reject queries that omit either.
- **`websearch_to_tsquery('english', :q)`** is the only permitted way to parse user/LLM-supplied text for BM25. `to_tsquery` over user input is forbidden.
- **RRF constants** (platform-fixed in v1): `k = 60`, `candidate_multiplier = 4`. With `limit=5`, the fusion pool is 40 rows.
- **Hybrid ranker behaviour:**
  - Pull top `N = candidate_multiplier × limit` from BM25 (`ts_rank_cd(content_tsv, websearch_to_tsquery(…))`) AND from vector search (`content_vec <=> :query_vec` with `WHERE content_vec IS NOT NULL`).
  - For each doc `d` in the union: `rrf_score(d) = 1/(k + bm25_rank(d)) + 1/(k + vector_rank(d))`. Treat missing rank as `+∞` (that side contributes `0`).
  - Sort by `rrf_score DESC`, tiebreak `created_at DESC`.
  - Return top `limit`.
- **Pure modes:** `mode=text` → top `limit` by BM25 alone; `mode=vector` → top `limit` by cosine alone.
- **Degrade behaviour:** If `pgvector` is unavailable OR the embedding provider call fails and `mode=hybrid` is requested → return BM25-only results with `ranking_used: "text"`. If `mode=vector` is explicitly requested under the same failure → return 503. Never silently fallback on an explicit mode.
- **Response fields on list/search results** include `memory_id`, `title`, `outcome`, `task_id`, `created_at`. Search adds `summary_preview` (first ~200 chars) and `score`. Full entry (detail endpoint) adds `summary`, `observations`, `tags`, `summarizer_model_id`, `updated_at`.
- **`agent_storage_stats`** (first page of list only): `entry_count` (exact COUNT on the agent's rows) and `approx_bytes` (either `pg_total_relation_size` style approximation, or `SUM(pg_column_size(…))` over the agent's rows — pick the cheaper one that gives a stable order-of-magnitude figure; document which in a comment on the query).
- **404-not-403:** List filters that miss, single-entry lookup by missing id, delete by missing id, search when the path `agent_id` does not exist — all return a uniform 404-shape response with a generic message. Do not include hints that an id exists in another scope.
- **Observability:** Every call to the search endpoint emits one `memory.search.served` structured log line with `tenant_id`, `agent_id`, `mode_requested`, `ranking_used`, `latency_ms`, `result_count`, `q_length`. When an embedding is computed, also emit one `memory.search.embedding` line with `tenant_id`, `agent_id`, `tokens`, `cost_microdollars`.

## Affected Component

- **Service/Module:** API Service — Agents / Memory
- **File paths:**
  - `services/api-service/.../controller/MemoryController.java` (new)
  - `services/api-service/.../service/MemoryService.java` (new)
  - `services/api-service/.../repository/MemoryRepository.java` (new)
  - `services/api-service/.../model/response/MemoryEntryResponse.java` (new — full detail)
  - `services/api-service/.../model/response/MemoryEntrySummary.java` (new — list + search items)
  - `services/api-service/.../model/response/MemoryListResponse.java` (new — items + `next_cursor` + first-page `agent_storage_stats`)
  - `services/api-service/.../model/response/MemorySearchResponse.java` (new — results + `ranking_used`)
  - `services/api-service/.../service/observability/MemoryLogger.java` (new — structured log helpers for `memory.search.*`)
  - Integration tests under the existing `services/api-service/src/test/` tree
- **Change type:** new code (mirrors the Track-4 Tool Server CRUD shape)

## Dependencies

- **Must complete first:** Task 1 (Migration + pgvector image pin) — the tables and indexes this API reads must exist.
- **Sequencing note for the orchestrator:** this task's Java-side embedding call uses a `provider_keys` row that Task 5 validates at startup. If Tasks 3 and 5 ship in parallel: Task 3's `mode=hybrid` / `mode=vector` integration tests must stub the provider until Task 5 lands and produces a valid key. The endpoint code path is Task-3-owned; only the runtime key availability depends on Task 5.
- **Provides output to:** Task 7 (worker `memory_search` tool delegates here), Task 9 (Console Memory tab), Task 10 (Console Submit attach widget via list endpoint).
- **Shared interfaces/contracts:** The four endpoints' response shapes.
- **Parallel-safety:** Task 4 also edits api-service Java files. If dispatched concurrently with Task 4, use `isolation: "worktree"` on one of them per AGENTS.md §Parallel Subagent Safety.

## Implementation Specification

### Embedding integration for the search endpoint

The search endpoint (for `mode=hybrid` and `mode=vector`) needs to embed the query `q`. There are two reasonable designs:

1. **API-side helper** — a thin Java wrapper around the embedding provider call using the same key `provider_keys` row the worker reads. Keeps the search path self-contained.
2. **Delegate to a worker-owned helper** — out of scope for v1 because the worker helper is Python.

Go with option 1 — a Java-side embedding call keyed off the existing `provider_keys` row. The call should:

- Be a bounded, single-shot HTTPS request to the embedding provider (default `text-embedding-3-small`, 1536 dims).
- Have a short timeout (<= 5 s) and a small retry budget (1 retry).
- On failure / timeout, degrade per the "Degrade behaviour" rules above.

Task 5 delivers the Python-side helper for the worker write / tool path — the two paths intentionally share no code in v1 because they live in different services. The embedding dimension and model choice are identical.

### Ranking SQL shape

For `mode=hybrid`, use a CTE / subquery structure:

```
WITH
  scoped AS (SELECT …  FROM agent_memory_entries
             WHERE tenant_id = :t AND agent_id = :a AND <filters>),
  bm25 AS (SELECT memory_id,
                  row_number() OVER (ORDER BY ts_rank_cd(content_tsv, q) DESC) AS bm25_rank
           FROM scoped, websearch_to_tsquery('english', :q) q
           WHERE content_tsv @@ q
           ORDER BY ts_rank_cd(content_tsv, q) DESC
           LIMIT :candidate_limit),
  vec AS  (SELECT memory_id,
                  row_number() OVER (ORDER BY content_vec <=> :qvec) AS vec_rank
           FROM scoped
           WHERE content_vec IS NOT NULL
           ORDER BY content_vec <=> :qvec
           LIMIT :candidate_limit)
SELECT scoped.*,
       (coalesce(1.0/(:k + bm25_rank), 0) + coalesce(1.0/(:k + vec_rank), 0)) AS rrf_score
FROM scoped
LEFT JOIN bm25 USING (memory_id)
LEFT JOIN vec  USING (memory_id)
WHERE bm25_rank IS NOT NULL OR vec_rank IS NOT NULL
ORDER BY rrf_score DESC, scoped.created_at DESC
LIMIT :limit
```

This is sketch, not copy-paste — adapt to the repository's existing JDBC/JPA/Spring Data style. Verify with `EXPLAIN` that the BM25 branch uses the GIN index and the vector branch uses the HNSW index.

For `mode=text`, omit the vec CTE and order by BM25 rank alone. For `mode=vector`, omit the bm25 CTE and order by `content_vec <=> :qvec` alone.

### List endpoint pagination

Cursor-based — use `(created_at DESC, memory_id DESC)` as the cursor. Return `next_cursor` as an opaque base64-encoded string. Existing list endpoints in the codebase (e.g., task list) already have a cursor helper — reuse it.

### Storage stats

- `entry_count`: exact `SELECT COUNT(*) FROM agent_memory_entries WHERE tenant_id = :t AND agent_id = :a;`.
- `approx_bytes`: `SELECT COALESCE(SUM(pg_column_size(title) + pg_column_size(summary) + pg_column_size(observations) + pg_column_size(tags) + 1536*4), 0) FROM …` — an order-of-magnitude number is sufficient. Document the choice with an inline comment citing the design doc "Scale and Operational Plan" section.
- Return these only on the first page (i.e., `cursor` absent) to avoid scanning the table on every pagination hop.

### Delete endpoint

- Hard delete by `memory_id` with the scope predicate.
- Return 204 on success.
- Do NOT cascade to `task_attached_memories` — per the design doc, attachment audit rows remain after memory deletion. Let the Task-1 schema do the right thing (no FK).
- Emit a `memory.delete.succeeded` structured log line.

### Authorisation

Use the existing tenant-scoping interceptor / filter that `AgentController` uses. Every response that is "scope miss" returns 404 with the same shape as `AgentController`'s "agent not found" path. Do NOT return `403` and do NOT expose "exists but not yours" information.

## Acceptance Criteria

- [ ] `GET /v1/agents/{agent_id}/memory` returns entries scoped to the caller's tenant + the path agent, sorted `created_at DESC`, with a `next_cursor` when more rows are available, and `agent_storage_stats` on the first page.
- [ ] `GET /v1/agents/{agent_id}/memory` with `outcome`, `from`, `to` filters narrows the result set correctly.
- [ ] `GET /v1/agents/{agent_id}/memory/search?q=…` with `mode=hybrid` returns results ranked by RRF (k=60, 4× candidate multiplier), with `ranking_used: "hybrid"`.
- [ ] `GET /v1/agents/{agent_id}/memory/search?q=…&mode=text` returns BM25-only results.
- [ ] `GET /v1/agents/{agent_id}/memory/search?q=…&mode=vector` returns cosine-similarity-only results.
- [ ] Search rejects `limit > 20` with a 400.
- [ ] Search does not return 500 on query strings that would fail `to_tsquery` parsing (e.g., unbalanced `"`, bare `&`, bare `|`). It returns a normal 200 with possibly empty results instead — implementation implication: use `websearch_to_tsquery('english', :q)`, never `to_tsquery`.
- [ ] When the embedding provider is simulated as down and `mode=hybrid`: endpoint returns BM25 results with `ranking_used: "text"`.
- [ ] When the embedding provider is simulated as down and `mode=vector`: endpoint returns 503.
- [ ] `GET /v1/agents/{agent_id}/memory/{memory_id}` returns the full entry for the owning scope; any scope miss returns 404 (uniform shape).
- [ ] `DELETE /v1/agents/{agent_id}/memory/{memory_id}` returns 204, the row is gone, and any matching `task_attached_memories` rows REMAIN.
- [ ] Every endpoint's handler rejects requests where the path `agent_id` does not belong to the caller's tenant with a uniform 404.
- [ ] Structured log lines `memory.search.embedding` (with token count + cost) and `memory.search.served` (with latency + result count) are emitted on the search path.
- [ ] All new tests pass; existing agent-API tests pass unchanged.

## Testing Requirements

- **Controller / service unit tests:** scope predicate enforcement (both `tenant_id` and `agent_id` required), RRF ordering with synthetic data, pure-mode behaviour, 404-not-403 enforcement, filter handling, pagination.
- **Integration tests (test DB):** INSERT a small fixture of memory rows, query each endpoint, verify the returned set matches expected. Include a test where two agents share a tenant and ensure no cross-agent leak.
- **Fault-injection tests:** Mock the embedding provider call to throw / time out. Verify `mode=hybrid` degrades and `mode=vector` 503s.
- **Parse-safety test:** `q = "\""` must not 500 — `websearch_to_tsquery` handles it cleanly.
- **Performance sanity:** With 100 synthetic rows, a hybrid search returns within a reasonable p95 (document in the test; no hard SLA in v1).

## Constraints and Guardrails

- Do not introduce a new cost ledger row for search-time embeddings. Log only (`memory.search.embedding`). The ledger schema is unchanged in Track 5.
- Do not implement temporal decay, MMR, cross-encoder rerank, or per-agent tuning of `k` / weights — all explicitly deferred in the design doc.
- Do not expose any path that differentiates "unknown id" from "id exists but not yours".
- Do not implement per-agent / per-tenant rate limiting in this task; existing platform-level limits apply.
- Do not update the worker or Console in this task. Tasks 7, 9, 10 consume this API.
- Do not add new provider-credential tables — use the existing `provider_keys` row and treat the embedding provider key as equivalent to a chat-model key (per design doc "Embeddings" section).
- Do not trust any caller-supplied `tenant_id` in the payload — always take it from the authenticated context.

## Assumptions

- Task 1 has shipped; `agent_memory_entries` and `task_attached_memories` exist and `pgvector` is enabled.
- The tenant-scoping interceptor already exists and wires `tenant_id` onto the request context before the controller runs.
- The `provider_keys` table contains a row for the embedding provider (Task 5 extends discovery to validate this at startup; Task 3 can rely on its presence at runtime).
- The embedding dimension is `1536` (matches the `vector(1536)` column from Task 1). Do not query or dynamically adjust.

<!-- AGENT_TASK_END: task-3-memory-rest-api.md -->
