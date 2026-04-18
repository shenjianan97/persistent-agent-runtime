# Phase 2 Track 5 — Agent Memory: Orchestrator Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give every completed task a durable, distilled, customer-visible memory entry scoped to its agent, retrievable by explicit attachment (submission time) or agent tool call (`memory_search`, `task_history_get`) — never auto-injected.

**Architecture:** A single Postgres table `agent_memory_entries` (pgvector + tsvector, one row per `task_id`, UPSERT on follow-up/redrive) holds memory. A dedicated LangGraph `memory_write` node summarises + embeds on the successful path; the worker co-commits the row with `UPDATE tasks SET status='completed'` under lease validation. A separate dead-letter hook writes template-only entries when observations exist. Three new tools (`memory_note`, `memory_search`, `task_history_get`) are registered per-task, scope-bound from the worker's task context. A `task_attached_memories` join table records per-task explicit attachments; resolved content is injected into the initial prompt. Customer-facing surface: memory REST endpoints + Console "Memory" tab + Submit-page attach widget. Memory is opt-in per agent via `agent_config.memory.enabled`.

**Tech Stack:** PostgreSQL + `pgvector` 0.7 + HNSW + `tsvector` generated column (infra/schema); Spring Boot / Jackson (API + validation); Python asyncpg + LangGraph + LangChain (worker write path, tools); React/TypeScript (Console); embedding provider (default `text-embedding-3-small`, 1536-d).

---

## A1. Implementation Overview

Track 5 extends the Phase 1/2 runtime with:

1. Infrastructure & schema — pgvector image pin across dev/CI/test DB; migration `0011_agent_memory.sql` creating `agent_memory_entries` and `task_attached_memories` with generated `content_tsv` column and HNSW/GIN/btree indexes.
2. Agent config extension — `agent_config.memory` sub-object with `enabled`, `summarizer_model`, `max_entries`; Jackson-safe request mapping; validation at create/update.
3. Memory REST API — list, hybrid search (RRF k=60, 4× candidate multiplier), detail, delete, plus `agent_storage_stats` on the first list page.
4. Task submission extension — `attached_memory_ids` (validated and persisted to `task_attached_memories`, echoed into `task_submitted` event) and `skip_memory_write` flag on `POST /v1/tasks`; detail response exposes `attached_memory_ids` + `attached_memories_preview`.
5. Worker embedding integration — provider abstraction + discovery-time key validation + `compute_embedding()` helper used at write and search time.
6. Worker memory write path — `MemoryEnabledState` schema, `memory_write` LangGraph node on the `agent → END` branch, `pending_memory` handoff, worker UPSERT + FIFO trim under lease, summarizer-outage template fallback, budget carve-out.
7. Worker memory tools — `memory_note` (state-mutating), `memory_search` (REST-backed hybrid), `task_history_get` (bounded diagnostic); scope bound from task context.
8. Worker dead-letter hook, follow-up seeding, and attached-memory injection — template-only dead-letter writes, `cancelled_by_user` skip, seed `observations` from existing row on follow-up/redrive, inject resolved attached entries into initial prompt.
9. Console Memory tab — per-agent list + search + detail + delete + 80%-of-cap warning + storage stats.
10. Console Submit-page attach widget — multi-select of past entries when `agent.memory.enabled`, token-footprint indicator.
11. Integration / E2E tests + Playwright browser verification — covering every acceptance criterion in the design doc.

**Canonical design contract:** `docs/design-docs/phase-2/track-5-memory.md` (Phase 2 design.md §3 is the obsolete original sketch, kept for history only).

---

## A2. Impacted Components

| Component | Path | Change Type | Description |
|-----------|------|-------------|-------------|
| Postgres image pin | `docker-compose.yml` (the `postgres` service), `Makefile` (`test-db-up`, `E2E_PG_IMAGE`), `.github/workflows/ci.yml` | modification | Swap `postgres:16` → `pgvector/pgvector:pg16` across dev, Makefile-driven test DB, and CI service containers |
| Database schema | `infrastructure/database/migrations/0011_agent_memory.sql` | new migration | `CREATE EXTENSION vector`; `agent_memory_entries` + `task_attached_memories`; generated `content_tsv`; HNSW + GIN + btree indexes |
| Agent config (API) | `services/api-service/.../model/request/MemoryConfigRequest.java`, `AgentConfigRequest.java`, `service/ConfigValidationHelper.java`, `service/AgentService.java` | new + modification | Nested `memory` sub-object; Jackson mapping; create/update validation; canonicalisation round-trip |
| Memory REST API | `services/api-service/.../controller/MemoryController.java`, `service/MemoryService.java`, `repository/MemoryRepository.java`, `model/response/MemoryEntry*.java` | new code | `GET/DELETE /v1/agents/{agent_id}/memory*` with hybrid RRF search and storage stats |
| Task submission (API) | `services/api-service/.../model/request/TaskSubmissionRequest.java`, `service/TaskService.java`, `controller/TaskController.java`, `repository` (new `TaskAttachedMemoryRepository.java`) | modification + new code | `attached_memory_ids`, `skip_memory_write`; scope-validated resolve; `task_attached_memories` inserts; event details; task detail response fields |
| Worker embedding | `services/worker-service/executor/embeddings.py`, `services/model-discovery/*` | new + modification | Provider abstraction + discovery-time validation of embedding key alongside chat-model keys |
| Worker write path | `services/worker-service/executor/memory_graph.py`, `executor/graph.py`, `core/memory_repository.py` | new + modification | `MemoryEnabledState`, `memory_write` node, post-astream commit, FIFO trim, fallback, budget carve-out |
| Worker memory tools | `services/worker-service/tools/memory_tools.py`, `executor/graph.py` (registration), `tools/definitions.py` (catalog) | new + modification | `memory_note` / `memory_search` / `task_history_get`; scope binding from task context; gating on `memory.enabled` and `skip_memory_write` |
| Worker dead-letter + follow-up + attach | `services/worker-service/executor/graph.py`, `executor/memory_graph.py`, `services/worker-service/core/worker.py` | modification | Template-only dead-letter write; `cancelled_by_user` skip; follow-up/redrive observation seeding; initial-prompt injection of attached entries |
| Console — Memory tab | `services/console/src/features/agents/memory/*`, `features/agents/AgentDetailPage.tsx` | new + modification | List + search + detail + delete + 80%-warning; storage stats; route `/agents/:id/memory` |
| Console — Submit attach | `services/console/src/features/submit/*` | modification | Multi-select memory picker, token-footprint indicator, pass `attached_memory_ids` through submit payload |
| Integration tests | `services/worker-service/tests/`, `tests/backend-integration/`, Playwright `CONSOLE_BROWSER_TESTING.md` scenario addition | new + modification | Acceptance-criteria coverage across API, worker, and Console |

---

## A3. Dependency Graph

```
Task 1 (Infra + Migration) ──┬──► Task 2 (Agent Config)  ──────────────────────────┐
                              │                                                      │
                              ├──► Task 3 (Memory REST API)  ────────────┬──► Task 9 (Console Memory tab) ──┐
                              │                                           │                                  │
                              ├──► Task 4 (Task Submission Ext)  ─────────┼──► Task 10 (Console Submit)  ────┤
                              │                                           │                                  │
                              ├──► Task 5 (Worker Embeddings)  ───────────┤                                  │
                              │                                           │                                  │
                              │               Task 2 + 5 ──► Task 6 (Worker Memory Write Path)  ─┐          │
                              │                                                                   │          │
                              │               Task 3 + 6 ──► Task 7 (Worker Memory Tools)  ──────┤          │
                              │                                                                   │          │
                              └── Task 4 + 6 ────────────────► Task 8 (DL + Follow-up + Attach) ─┴──────────┴──► Task 11 (E2E + Browser)
```

**Parallelisation opportunities:**

- Task 1 must land first — everything else blocks on migration and pgvector image.
- After Task 1: Tasks 2, 3, 4, 5 can all proceed in parallel. Tasks 2, 3, 4 all edit api-service Java source (different subpaths but same package tree); if any two are parallelised, dispatch under `isolation: "worktree"` per AGENTS.md §Parallel Subagent Safety.
- Task 6 depends on Task 2 (config shape, gating) and Task 5 (embedding helper).
- Task 7 depends on Task 3 (search API it delegates to) and Task 6 (state schema, registration hook). Data-flow: Task 7 writes the observations that Task 8's dead-letter hook reads.
- Task 8 depends on Task 4 (join table + submission payload) and Task 6 (write path to branch from); data-flow dep on Task 7.
- Task 9 depends on Tasks 2 and 3; Task 10 depends on Tasks 3 and 4. Tasks 9 and 10 both edit Console — if run in parallel use worktree isolation.
- Tasks 6, 7, 8 all edit `services/worker-service/executor/graph.py` — if parallelised, use worktree isolation on at least N-1 of them.
- Task 11 depends on everything before it.

Follow **AGENTS.md §Parallel Subagent Safety** — any time two tasks touch the same file (e.g., Tasks 6/7/8 all edit `graph.py`; Tasks 9/10 both edit Console), use `isolation: "worktree"` on one or more of the agents and merge on completion.

---

## A4. Data / API / Schema Changes

**New tables:** `agent_memory_entries` (one row per `task_id`, UPSERT), `task_attached_memories` (join with `ON DELETE CASCADE` from tasks, no FK from memory_id so `DELETE` of a memory entry leaves the attachment audit intact). Both additive; no existing row mutations.

**New extension:** `pgvector` (required by the migration — fails hard on image without it).

**`agents.agent_config`:** Additive JSONB sub-object `memory { enabled, summarizer_model, max_entries }`; absent or `enabled=false` preserves Phase-1-style behaviour exactly.

**`POST /v1/tasks` payload:** Adds `attached_memory_ids: uuid[]` and `skip_memory_write: bool` — both optional, default absent / `false`. Backward compatible.

**Task detail response:** Adds `attached_memory_ids: uuid[]` and `attached_memories_preview: [{memory_id, title}]`. Backward compatible.

**`task_submitted` event `details` JSONB:** Adds `attached_memory_ids` key mirroring the join table (join is authoritative on divergence).

**Memory REST API:** New resource at `/v1/agents/{agent_id}/memory` with list, search, detail, delete.

**Cost ledger:** Existing `agent_cost_ledger` is used for write-time summarizer + embedding; search-time embeddings via REST are logged (`memory.search.embedding` structured log) but NOT inserted into the ledger (schema unchanged — avoids nullable `task_id`/`checkpoint_id` migration for v1).

---

## A4.1. Task Handoff Outputs

| Task | Output |
|------|--------|
| Task 1 | Migration `0011_agent_memory.sql` applied on fresh PG; pgvector extension present in dev/CI/test DB containers; `agent_memory_entries` and `task_attached_memories` queryable |
| Task 2 | `agent_config.memory` sub-object persisted + validated + canonicalised; Jackson no longer drops unknown `memory` field; summarizer_model cross-checked against `models` |
| Task 3 | `GET /v1/agents/{agent_id}/memory`, `/memory/search`, `/memory/{memory_id}`, and `DELETE /memory/{memory_id}` with RRF hybrid ranking, `agent_storage_stats`, 404-shape disclosure rule |
| Task 4 | `POST /v1/tasks` accepts + validates + persists `attached_memory_ids` and honours `skip_memory_write`; task detail response exposes attachment fields; event mirrors the list |
| Task 5 | `compute_embedding(text)` helper + provider key validation at model-discovery startup; deferred-embedding path when provider is down |
| Task 6 | `memory_write` LangGraph node inside a memory-enabled graph; worker UPSERT + FIFO trim + lease-validated commit; template-fallback on summarizer outage; budget carve-out |
| Task 7 | `memory_note`, `memory_search`, `task_history_get` tools registered per-task with scope bound from task context; gated by effective memory state |
| Task 8 | Dead-letter hook writes template-only entry when observations exist; `cancelled_by_user` writes nothing; follow-up/redrive seed observations from existing row; attached entries injected into initial prompt |
| Task 9 | Console Memory tab on Agent detail with list, search, detail, delete, 80%-warning banner, storage stats |
| Task 10 | Submit page multi-select memory picker when `agent.memory.enabled`, token-footprint indicator, `attached_memory_ids` wired into submission |
| Task 11 | E2E covering the 15 design acceptance criteria + Playwright Memory-tab scenario + Submit-page attach scenario |

---

## A5. Integration Points

| Caller | Callee | Interface Change | Failure Handling |
|--------|--------|-------------------|-----------------|
| Memory API | `agent_memory_entries` | New tenant+agent-scoped reads, writes, hybrid search | Scope mismatch → uniform 404 (404-not-403 rule) |
| Memory API | embedding provider (via worker-owned helper or API-side call — see Task 3) | Embed query at search time | Provider down + `mode=vector` → 503; `mode=hybrid` → silent degrade to text with `ranking_used:"text"` |
| Task API | `task_attached_memories` + `agent_memory_entries` | Single scoped `WHERE memory_id = ANY($1) AND tenant_id = :caller AND agent_id = :path_agent` for resolution | Any miss → uniform 4xx (no "unknown"/"wrong-tenant"/"wrong-agent" differentiation) |
| Worker `memory_write` node | summarizer LLM | Title + summary generation | After internal retries fail → template fallback entry with `summarizer_model_id='template:fallback'` |
| Worker `memory_write` node | embedding provider | Embed `title + summary + observations + tags` | Provider down → `content_vec=NULL`; row still written |
| Worker commit | `agent_memory_entries` + `tasks` | Single tx: UPSERT memory row + UPDATE tasks status + lease validation + FIFO trim if over cap | Lease validation failure → rollback; reaper re-claim resumes from last checkpoint |
| Worker dead-letter hook | `agent_memory_entries` | Template write when observations non-empty + not cancelled | Lease validation on the `UPDATE tasks` ensures no orphan memory row |
| Worker `memory_search` tool | Memory REST API (internal) | Hybrid search scoped to task context | Embedding provider down → degrade to `mode=text`; scope miss → tool error, graph stays in-loop |
| Worker `task_history_get` tool | `tasks` + `agent_memory_entries` | Bounded fields, cross-agent/tenant miss returns tool error | Miss returns tool-shaped "not found" |
| Console | Memory REST API | New list/search/detail/delete endpoints | Error toast; 404 uniform |
| Console Submit | Memory API + Task submission | Attachment picker + `attached_memory_ids` in payload | Validation error surfaces on submit |

---

## A6. Deployment and Rollout

Same single-deployment pattern as Tracks 1–4. Key sequencing:

1. **pgvector image ships first.** Without `pgvector/pgvector:pg16` (or an RDS instance with `vector` enabled), the migration fails on `CREATE EXTENSION vector`. Roll this to dev/CI/test-DB containers before (or atomically with) the migration PR.
2. **Migration `0011_agent_memory.sql`** is picked up by the existing migration glob (`[0-9][0-9][0-9][0-9]_*.sql`) — no CI workflow changes required for that alone.
3. **New services are backward-compatible.** Without `memory.enabled=true` on an agent, no column is read, no tool is registered, no graph node is added — Phase-1/2 behaviour is preserved exactly.
4. **Production Postgres validation:** Before rollout, confirm the production / staging Postgres can `CREATE EXTENSION vector` (deploy role needs superuser or pre-installed extension). This is a **deploy blocker** — verify during Task 1.
5. **`make db-reset`** applies all migrations including `0011` for local development. Dev data is expected to be wiped.

---

## A7. Observability

- Structured log lines: `memory.write.started`, `memory.write.committed`, `memory.write.template_fallback`, `memory.write.embedding_failed`, `memory.write.trim_evicted`, `memory.search.embedding`, `memory.search.served`, `memory.deadletter.template`, `memory.attach.injected`.
- All emit `tenant_id`, `agent_id`, `task_id` (where applicable), latency, and (for summarizer/embedding) token count + cost in microdollars.
- `agent_cost_ledger` rows for write-time summarizer and embedding — attributed to the originating task's current checkpoint.
- Latency instrumentation for `memory_write` node (summarizer + embedding + commit) and `memory_search` p50/p95 per mode.
- Per-agent entry count + approximate bytes, exposed via `agent_storage_stats` in the list response and logged at task completion.
- HNSW `ef_search` effective value and deferred-embedding backlog (`content_vec IS NULL` per agent) — metrics only for now, dashboards deferred.

---

## A8. Risks and Open Questions

| Risk | Mitigation |
|------|-----------|
| `CREATE EXTENSION vector` requires superuser on managed Postgres | Task 1 confirms deploy role before shipping; image pin handles dev/CI/test DB |
| HNSW index build or query is slow at scale | Per-agent soft cap (10k, platform max 100k) with FIFO trim on INSERT; partial indexes and partitioning deferred to later phase |
| Summarizer or embedding provider outage | Template fallback for summarizer; deferred `content_vec=NULL` for embedding; invariant "every completed memory-enabled task produces exactly one entry" preserved |
| Budget enforcement pausing a task mid-memory-write | Budget carve-out named by graph-node identity (`memory_write`) — skip per-task pause check while preserving hourly-spend accounting |
| Cross-tenant leakage via HNSW neighbours | Every query includes `tenant_id` and `agent_id` predicates; repository-layer static check enforced at review |
| Follow-up/redrive overwriting first-execution observations | Worker seeds `observations` from the existing row before the graph resumes; observations + summary merged on commit |
| Stale memory entry after agent deletion | Soft-delete agents; memory rows stay valid; no cascade beyond task deletion (which itself is unusual in Phase 2) |
| Tool search arguments broadening scope | Tool scope bound from immutable task context at registration, never from LLM arguments; SQL appended server-side |
| `to_tsquery` crash on arbitrary LLM input | Every query uses `websearch_to_tsquery('english', :q)` — raw `to_tsquery` forbidden |
| Enum drift on `dead_letter_reason` | Reuse the Phase 2 Track 2 value `cancelled_by_user`; no new reasons introduced |
| Memory entry lost on worker crash between summarizer LLM and commit | Checkpointer persists `pending_memory`; reaper re-claim resumes graph, UPSERT absorbs any duplicate |

---

## A9. Orchestrator Guidance

- Use `docs/design-docs/phase-2/track-5-memory.md` as the canonical design contract. Phase 2 `design.md §3` is historical and **must not** be followed.
- Memory-disabled agents (`memory.enabled=false` or absent) and tasks submitted with `skip_memory_write=true` MUST behave identically to pre-Track-5 behaviour. No new tools registered, no new graph node, no new cost, no new rows written.
- Always include both `tenant_id` and `agent_id` predicates in every query touching `agent_memory_entries` or `task_attached_memories`. Enforce at the repository layer; reviewers must reject PRs that violate this.
- Follow the 404-not-403 disclosure rule across every memory-related surface (list, single-entry lookup, search, delete, tool errors). Uniform response shape regardless of whether the id is unknown, from another tenant, or from another agent.
- Attachments are **immutable after task creation**. Follow-up and redrive do not rewrite the join table.
- `memory_note` observations are append-only and durable at super-step checkpoint granularity. The `operator.add` reducer is required on the `observations` state field.
- The `memory_write` node fires only on the agent's chosen terminal path (no pending tool calls, `agent → memory_write → END`). It must NOT fire on HITL pauses, budget pauses, or any other non-terminal state.
- Memory UPSERT preserves `created_at` immutably; `updated_at` and `version` advance on every write.
- FIFO trim runs in the same transaction as the UPSERT insert branch only. `ON CONFLICT DO UPDATE` (follow-up/redrive) does NOT trigger trim.
- Write path: summarizer LLM cost → `agent_cost_ledger`, exempt from `budget_max_per_task`, counted in `budget_max_per_hour`. Embedding cost (write and search) is zero-rated in v1; search-time embeddings are logged, not ledgered.
- Tool scope binding comes from the worker's task context at registration time, never from LLM arguments. Applies equally to `task_history_get`, even though it is always available.
- Do NOT implement: auto-loading of memory, tiered in-task compaction (that's Track 7), customer-supplied embedding providers, BYO memory backends, cross-agent or cross-tenant sharing, retention/decay, auto-promotion/compaction, raw-trace search, summary regeneration endpoint.
- Do NOT introduce a new dead-letter reason. Cancellation is `status='dead_letter' AND dead_letter_reason='cancelled_by_user'` (from Phase 2 Track 2).
- Console Memory tab and Submit-page attach widget are **blocking** — every Console change requires Playwright verification per AGENTS.md §Browser Verification.

---

## A10. Key Design Decisions

1. **Hybrid RRF, k=60, 4× candidate multiplier, platform-level constants in v1.** Reciprocal Rank Fusion is the widely-adopted standard; tuning is deferred until data justifies it.
2. **One entry per `task_id` via `UNIQUE (task_id)` + UPSERT.** Follow-up and redrive overwrite the prior entry; `created_at` is immutable, `updated_at` and `version` advance.
3. **Hybrid graph-node + worker-commit architecture.** The summarizer LLM call runs inside a LangGraph node (`memory_write`) so LangGraph checkpointer handles crash recovery for the expensive step. The database commit is owned by the worker's post-astream path, co-committed with `UPDATE tasks SET status='completed'` in one lease-validated transaction.
4. **Template-fallback preserves the "one entry per completed task" invariant.** Summarizer outage does not skip the write — the node populates `pending_memory` from a template and the worker still commits.
5. **Memory is never auto-injected.** Three retrieval paths, all explicit: customer attach at submission, agent `memory_search` tool, Console browse/attach. Durable agent rules stay in the system prompt.
6. **Budget carve-out is narrow and named.** The `budget_max_per_task` pause check skips specifically for the `memory_write` graph node (identified by node name). Hourly spend still accrues.
7. **Scope binding from task context, not LLM args.** All three tools filter by the worker's bound `(tenant_id, agent_id)`; LLM-controlled arguments can only narrow within scope, never broaden it.
8. **FIFO trim as soft cap, not retention policy.** 10k-entry default, 100k hard max. Trim runs only on the INSERT branch of the UPSERT, never the UPDATE branch.
9. **pgvector image swap across dev, Makefile-driven test DB, and CI.** The Makefile path (`E2E_PG_IMAGE` in `test-db-up`) is a separate code path from `docker-compose.yml` and must be updated explicitly.
10. **Jackson-safe agent config extension.** Spring Boot defaults to `FAIL_ON_UNKNOWN_PROPERTIES=true`; `AgentConfigRequest` and `canonicalizeConfig` must be extended or the `memory` field round-trips to null.
11. **Cost ledger schema unchanged.** Write-time LLM + embedding calls ride the existing `task_id` + `checkpoint_id` schema. Search-time API-driven embeddings are logged, not inserted — avoids a nullable-FK migration for v1.

---

## B. Agent Task Files

| Task | File | Description |
|------|------|-------------|
| Task 1 | [task-1-migration-and-pgvector.md](agent_tasks/task-1-migration-and-pgvector.md) | Migration `0011_agent_memory.sql`, pgvector image pin across dev / Makefile / CI |
| Task 2 | [task-2-agent-config-extension.md](agent_tasks/task-2-agent-config-extension.md) | `agent_config.memory` sub-object: Jackson mapping, validation, canonicalisation |
| Task 3 | [task-3-memory-rest-api.md](agent_tasks/task-3-memory-rest-api.md) | `GET/DELETE /v1/agents/{agent_id}/memory*` with RRF hybrid search and storage stats |
| Task 4 | [task-4-task-submission-extension.md](agent_tasks/task-4-task-submission-extension.md) | `attached_memory_ids` + `skip_memory_write`; `task_attached_memories` inserts; event details |
| Task 5 | [task-5-worker-embeddings.md](agent_tasks/task-5-worker-embeddings.md) | Embedding provider abstraction + model-discovery validation + deferred-embedding path |
| Task 6 | [task-6-worker-memory-write.md](agent_tasks/task-6-worker-memory-write.md) | `memory_write` graph node + worker commit + FIFO trim + template fallback + budget carve-out |
| Task 7 | [task-7-worker-memory-tools.md](agent_tasks/task-7-worker-memory-tools.md) | `memory_note`, `memory_search`, `task_history_get` tools with scope-bound context |
| Task 8 | [task-8-worker-deadletter-followup-attach.md](agent_tasks/task-8-worker-deadletter-followup-attach.md) | Dead-letter template hook, follow-up/redrive observation seeding, attached-memory injection |
| Task 9 | [task-9-console-memory-tab.md](agent_tasks/task-9-console-memory-tab.md) | Memory tab on Agent detail: list, search, detail, delete, storage stats, 80% warning |
| Task 10 | [task-10-console-submit-attach.md](agent_tasks/task-10-console-submit-attach.md) | Submit page multi-select memory picker + token-footprint indicator |
| Task 11 | [task-11-integration-and-browser-tests.md](agent_tasks/task-11-integration-and-browser-tests.md) | E2E coverage of all 15 acceptance criteria + Playwright scenarios |
