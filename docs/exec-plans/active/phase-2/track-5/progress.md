# Phase 2 Track 5 — Agent Memory: Progress

| Task | Component | Status | Description |
|------|-----------|--------|-------------|
| Task 1 | Infra + Migration | Done | Migration `0011_agent_memory.sql`; pgvector image pin across dev/CI/test DB |
| Task 2 | Agent Config Extension | Not started | `agent_config.memory` sub-object: Jackson, validation, canonicalisation |
| Task 3 | Memory REST API | Not started | List, hybrid RRF search, detail, delete, storage stats |
| Task 4 | Task Submission Extension | Not started | `attached_memory_ids` + `skip_memory_write`, join table, event mirror |
| Task 5 | Worker Embeddings | Not started | Provider abstraction + discovery validation + deferred path |
| Task 6 | Worker Memory Write Path | Not started | `memory_write` node + commit + trim + template fallback + budget carve-out |
| Task 7 | Worker Memory Tools | Not started | `memory_note`, `memory_search`, `task_history_get` with scope binding |
| Task 8 | DL + Follow-up + Attach | Not started | Template DL hook, observation seeding, prompt injection |
| Task 9 | Console — Memory Tab | Not started | List, search, detail, delete, storage stats, 80% warning |
| Task 10 | Console — Submit Attach | Not started | Multi-select picker, token-footprint indicator |
| Task 11 | Integration + Browser | Not started | 15-criterion E2E coverage + Playwright scenarios |

## Notes

- Canonical design contract: `docs/design-docs/phase-2/track-5-memory.md`. The original `design.md §3` sketch is historical only.
- Memory is **opt-in per agent** (`agent_config.memory.enabled`, default `false`). Every task must verify Phase-1/2 behaviour is preserved when memory is disabled.
- Tasks 3 and 4 both edit API-service Java files; Tasks 6/7/8 all edit worker `executor/graph.py`; Tasks 9 and 10 both edit Console. Run these in parallel only with `isolation: "worktree"` per AGENTS.md §Parallel Subagent Safety.
- pgvector availability on the deploy-time Postgres (production / staging) is a release blocker. Confirm during Task 1.
