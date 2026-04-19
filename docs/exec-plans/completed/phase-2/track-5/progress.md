# Phase 2 Track 5 — Agent Memory: Progress

| Task | Component | Status | Description |
|------|-----------|--------|-------------|
| Task 1 | Infra + Migration | Done | Migration `0011_agent_memory.sql`; pgvector image pin across dev/CI/test DB |
| Task 2 | Agent Config Extension | Done | `agent_config.memory` sub-object: Jackson, validation, canonicalisation |
| Task 3 | Memory REST API | Done | List, hybrid RRF search, detail, delete, storage stats |
| Task 4 | Task Submission Extension | Done | `attached_memory_ids` + `skip_memory_write`, join table, event mirror |
| Task 5 | Worker Embeddings | Done | Provider abstraction + discovery validation + deferred path |
| Task 6 | Worker Memory Write Path | Done | `memory_write` node + commit + trim + template fallback + budget carve-out |
| Task 7 | Worker Memory Tools | Done | `memory_note`, `memory_search`, `task_history_get` with scope binding |
| Task 8 | DL + Follow-up + Attach | Done | Template DL hook, observation seeding, prompt injection |
| Task 9 | Console — Memory Tab | Done | List, search, detail, delete, storage stats, 80% warning |
| Task 10 | Console — Submit Attach | Done | Multi-select picker, token-footprint indicator |
| Task 11 | Integration + Browser | Done (subagent half) | 15-criterion E2E coverage manifest (`services/worker-service/tests/test_track5_ac_mapping.py`) + new REST E2E tests (`tests/backend-integration/test_memory_task_submission.py`) + Playwright Scenarios 11/12/13. Orchestrator still owns live Playwright execution per AGENTS.md §Browser Verification. |
| Task 12 | Task Memory Mode | Done | `skip_memory_write` boolean fully removed; three-value `memory_mode` enum (`always` / `agent_decides` / `skip`) surfaced on `POST /v1/tasks` and task detail; new `save_memory(reason)` tool registered only in `agent_decides`; conditional `route_after_agent` wiring + per-run `memory_opt_in` reset; Console `<Select>` with disabled-when-memory-off branch. Migration numbered **0013** (0012 was already taken by `0012_memory_check_constraints.sql`). E2E + worker + Console + API unit suites all green; orchestrator ran Playwright nav smoke + memory-mode dropdown verification (render, disabled branch, 400 on crafted always-against-memory-off, task detail surfaces `memory_mode`). See `agent_tasks/task-12-task-memory-mode.md`. |

## Notes

- Canonical design contract: `docs/design-docs/phase-2/track-5-memory.md`. The original `design.md §3` sketch is historical only.
- Memory is **opt-in per agent** (`agent_config.memory.enabled`, default `false`). Every task must verify Phase-1/2 behaviour is preserved when memory is disabled.
- Tasks 3 and 4 both edit API-service Java files; Tasks 6/7/8 all edit worker `executor/graph.py`; Tasks 9 and 10 both edit Console. Run these in parallel only with `isolation: "worktree"` per AGENTS.md §Parallel Subagent Safety.
- pgvector availability on the deploy-time Postgres (production / staging) is a release blocker. Confirm during Task 1.
- **Task 4 bug surfaced during Task 10 browser verification:** `TaskAttachedMemoryRepository.findAttachedMemoriesPreview` passes JDBC parameters in the wrong order (`taskId, tenantId, agentId` instead of `tenantId, agentId, taskId` matching the SQL), causing a PostgreSQL `operator does not exist: text = uuid` error on any `POST /v1/tasks` that attaches memory ids. Submissions with `skip_memory_write=true` and no attachments succeed; attached-memory submissions fail with HTTP 500. Task 10's Console code is correct — request body shape and selection order verified via `browser_network_requests`. The fix belongs with Task 4; file a follow-up.
  - **Status:** already fixed on `main` in commit 9395137 ("Fix Task 4 + Task 10 merge fallout") — parameter order now matches the SQL `?, ?, ?` placeholder order (`tenantId, agentId, taskId`). Task 11's new E2E test `test_ac8_attach_valid_persists_in_join_table_and_event` runs against the live API + attached ids successfully, confirming the fix in flight.
- **Task 11 subagent deliverables (this commit):**
  - `tests/backend-integration/test_memory_task_submission.py` — 10 new REST E2E tests covering AC-1, AC-8, AC-10, AC-11 via the live API + isolated DB.
  - `services/worker-service/tests/test_track5_ac_mapping.py` — 17 manifest/meta-tests that bind each of the 15 ACs to concrete tests and fail if a referenced test file moves or disappears.
  - `tests/backend-integration/helpers/api_client.py` — `submit_task()` now plumbs `attached_memory_ids` and `skip_memory_write` (opt-in kwargs).
  - `docs/CONSOLE_BROWSER_TESTING.md` — Scenarios renumbered (Submit-Attach is now Scenario 12), added Scenario 13 "Memory End-to-End Cross-Feature Flow" covering Task 11's Memory-Tab-E2E + Submit-Attach-E2E combined walkthrough. AC mapping noted inline in each scenario.
- **Browser verification remaining:** Orchestrator must execute Playwright Scenarios 1, 11, 12, 13 (the "Cross-cutting memory feature / Track 5 verification" row) against `make start` before flipping Track 5 to "Done" in STATUS.md and moving this directory to `completed/`. Subagent does not run `make start` or Playwright MCP tools per AGENTS.md §Browser Verification.
- **Task 12 orchestrator fixes (merged on top of subagent worktrees in `cbafee2`):**
  - `ConfigValidationHelper.validateMemoryModeAgainstAgent` cast `agent_config` directly to `String`, but the JDBC driver hands jsonb back as `PGobject`. Pre-fix, every `POST /v1/tasks` 500'd against the live DB even though unit tests (mocked) passed. Fix: unwrap PGobject via a shared helper + regression unit test that seeds a `PGobject` through the mock.
  - `TaskService.submitTask` now resolves the `memory_mode` default from the agent's `memory.enabled` when the caller omits the field (memory-enabled → `"always"`, memory-disabled → `"skip"`). The spec's literal `default=always` would have forced every Phase-1/2 test and every legacy caller to start sending `memory_mode='skip'` against memory-disabled agents, violating the "Phase-1/2 behaviour preserved when memory is disabled" invariant. Explicit values still go through the strict cross-field validator per spec.
  - `CONSOLE_BROWSER_TESTING.md` Scenarios 11 / 13 reconciled with the Console slice's testid change (`memory-attach-card` now renders for memory-disabled agents as a locked `skip` dropdown; the attach picker remains gated on `memory.enabled`).
