<!-- AGENT_TASK_START: task-11-integration-and-browser-tests.md -->

# Task 11 — Integration + E2E + Browser Verification

## Agent Instructions

You are a software engineer implementing one module of a larger system. Your scope is strictly limited to this task.

**CRITICAL PRE-WORK:** Before beginning implementation, you MUST read:
1. `docs/design-docs/phase-2/track-5-memory.md` — section "Acceptance Criteria" end-to-end. This task's scope is a one-to-one mapping of those 15 criteria to executable tests plus the Console Playwright scenarios.
2. `docs/CONSOLE_BROWSER_TESTING.md` — existing scenarios and the scenario-matrix. This task adds the Memory-tab and Submit-attach scenarios (if not already added by Tasks 9 / 10).
3. Tasks 1–10's output — the tables, endpoints, worker code, and Console views this task exercises.
4. `tests/backend-integration/` — conventions for cross-service E2E tests (Java API + Python worker + test DB).
5. `services/worker-service/tests/` and the existing integration test harness — how end-to-end tests wire the worker, embedder, and summarizer.

**CRITICAL POST-WORK:** After completing this task:
1. Run `make test-all` and confirm every new and existing test passes.
2. Run the full Playwright suite covering Scenario 1 (Navigation Smoke) + the Memory-tab scenario + the Submit-attach scenario.
3. Update `docs/exec-plans/active/phase-2/track-5/progress.md` to "Done" for this task, and mark any earlier task that had been left in "Almost Done" as fully "Done" if this task confirms it. **Update progress.md BEFORE the archival move in step 4 — the file moves with the directory.**
4. Move the entire `docs/exec-plans/active/phase-2/track-5/` directory to `docs/exec-plans/completed/phase-2/track-5/`.
5. Update `STATUS.md` to reflect Track 5 completion.

## Context

This task is the verification cap. Every acceptance criterion in the design doc gets at least one test that exercises the real components (not mocks) in a realistic flow. Playwright scenarios cover the Console surfaces. Unit-level regressions are covered earlier in Tasks 1–10; this task is about **cross-component integration** and **browser verification**.

The 15 design-doc acceptance criteria (paraphrased for reference — the canonical list lives in the design doc):

1. Agent opt-in + max_entries default persisted.
2. Every completed memory-enabled task writes exactly one entry (summarizer outage → template fallback; invariant preserved).
3. Dead-letter with observations → template `failed` entry; cancelled_by_user → no write; observations-empty → no write.
4. Follow-up + redrive overwrite; `created_at` preserved; `updated_at`+`version` advance; observations seeded from the existing row.
5. `memory_note` appends observations; durable at super-step checkpoint granularity; visible in the final entry.
6. `memory_search` returns RRF hybrid results scoped to `(tenant_id, agent_id)`; scope bound from worker context, not LLM args.
7. `task_history_get` returns a bounded view; cross-agent / cross-tenant task ids return tool-shaped "not found".
8. Attachment at submission validated in a single scoped query; resolution miss returns uniform 4xx; persisted in `task_attached_memories` (with `position`); mirrored into `task_submitted` event details.
9. Customer can browse, search, read, and delete via Console + API; `agent_storage_stats` exposed.
10. Cross-tenant / cross-agent access uniformly 404.
11. Memory-disabled (agent-level or `skip_memory_write`) → no tool registration, no `memory_write` node, no rows, no cost; `task_history_get` still available.
12. Memory write does not block completion: summarizer outage → template; embedding outage → `content_vec=NULL`; task still `completed`.
13. `max_entries` FIFO trim on the INSERT branch; Console warning at 80%.
14. Summarizer + embedding write-time cost in `agent_cost_ledger`; summarizer exempt from `budget_max_per_task` enforcement; embedding zero-rated.
15. Unit + E2E cover: memory disabled, enabled + success, summarizer fallback, embedding fallback, dead-letter with + without observations, cancelled_by_user, follow-up + redrive overwrite + seeding, cross-tenant rejection, all three `memory_search` modes, `task_history_get` scope enforcement, `max_entries` trim, `skip_memory_write` at submission.

## Task-Specific Shared Contract

- **Test location:** prefer `tests/backend-integration/` for Java + worker + DB E2E; `services/worker-service/tests/` for worker-only integration (happy-path, fallback paths, crash-recovery); `services/api-service/src/test/` for API-only integration; `services/console/src/features/*/*.e2e.test.tsx` + Playwright scenarios for Console.
- **Every acceptance criterion (1–15) must map to at least one assertion.** Write a table in the PR description mapping criterion ↔ test file.
- **Playwright scenarios:** add to `docs/CONSOLE_BROWSER_TESTING.md` if not already present:
  - **Memory Tab E2E:** browse → search → detail → delete → attach-to-new-task → navigate back.
  - **Submit Attach E2E:** select agent → attach memories via picker → submit → task detail shows attachments → wait for completion → memory entry appears in Memory tab.
- **Fault-injection for fallback paths:** use mocks at the provider boundary (summarizer LLM, embedding provider) but real DB, real worker, real API. Do not mock the worker or the commit path.
- **Budget-carve-out test:** set `budget_max_per_task` low enough that the `memory_write` super-step would exceed it, but high enough that regular super-steps do not. The test asserts the task completes rather than pauses.
- **Crash-recovery test:** exploit a deterministic injection point (e.g., an env var or a monkeypatch the test harness recognises) to kill the worker between `memory_write` state commit and DB commit. Reaper re-claim completes the task with a single memory row.
- **Cross-tenant / cross-agent tests:** explicit two-tenant / two-agent setup; assertions that every memory-touching endpoint returns a uniform 404 for out-of-scope ids.

## Affected Component

- **Service/Module:** Integration + E2E + Console browser verification
- **File paths:**
  - `tests/backend-integration/track5/*.py` (new) — cross-service flows.
  - `services/worker-service/tests/test_track5_flows.py` (new or extension).
  - `services/api-service/src/test/java/.../MemoryIntegrationTest.java` (new or extension).
  - `services/console/src/features/agents/memory/*.e2e.test.tsx` (new if Vitest suite supports E2E) + Playwright scenario edits in `docs/CONSOLE_BROWSER_TESTING.md`.
  - `docs/exec-plans/completed/phase-2/track-5/` — move the whole active directory here on completion.
  - `STATUS.md` (modify — Track 5 entry flipped to "Done").
- **Change type:** new tests + archival move + status update

## Dependencies

- **Must complete first:** Tasks 1–10.
- **Provides output to:** Track 5 is completion-gated by this task.
- **Shared interfaces/contracts:** the tests themselves are the contract.

## Implementation Specification

### AC-to-test mapping (skeleton)

Produce a table in the PR description of the form:

| AC # | Scenario | Test file |
|------|----------|-----------|
| 1 | Agent opt-in + max_entries default | `MemoryIntegrationTest#agentOptInPersistsMaxEntriesDefault` |
| 2 | One entry per completed memory-enabled task | `track5/test_happy_path.py::test_one_entry_per_completed_task` |
| 2 | Summarizer outage → template fallback | `track5/test_fallbacks.py::test_summarizer_outage_template` |
| 3 | Dead-letter + observations → template | `test_memory_dead_letter.py::test_dead_letter_with_observations` |
| 3 | Cancelled-by-user → no write | `test_memory_dead_letter.py::test_cancelled_by_user_no_write` |
| 3 | Dead-letter + no observations → no write | `test_memory_dead_letter.py::test_dead_letter_no_observations_no_write` |
| 4 | Follow-up overwrites | `track5/test_followup.py::test_follow_up_overwrites` |
| 4 | Redrive overwrites | `track5/test_redrive.py::test_redrive_overwrites` |
| 4 | Seeding observations | `test_memory_follow_up_seeding.py::test_seeding_from_existing_row` |
| 5 | memory_note persists + survives | `track5/test_memory_note.py::test_memory_note_durable_across_checkpoint` |
| 6 | memory_search RRF hybrid + scope-bound | `track5/test_memory_search.py::test_hybrid_scope_bound` |
| 7 | task_history_get bounded + scope | `track5/test_task_history.py::test_cross_agent_returns_not_found` |
| 8 | Attachment valid path | `track5/test_attach.py::test_valid_attach_persists_and_injects` |
| 8 | Attachment rejection uniform shape | `track5/test_attach.py::test_cross_tenant_rejected_uniformly` |
| 9 | Console memory flow | Playwright: "Memory Tab E2E" |
| 9 | Storage stats | `MemoryIntegrationTest#listFirstPageIncludesStorageStats` |
| 10 | Cross-tenant uniform 404 | `MemoryIntegrationTest#crossTenantReturns404Uniformly` |
| 11 | skip_memory_write → no write, task_history_get still available | `track5/test_skip_flag.py` |
| 12 | Summarizer / embedding outage → task still completes | `track5/test_fallbacks.py` |
| 13 | FIFO trim | `track5/test_trim.py::test_trim_on_insert_branch_only` |
| 14 | Cost-ledger entries + budget carve-out | `track5/test_budget_carve_out.py` |
| 15 | All acceptance scenarios (meta) | coverage tracked across the table above |

Adjust file names and test names to match your actual suite layout.

### Playwright scenarios

Append to `docs/CONSOLE_BROWSER_TESTING.md`:

- **Scenario: Memory Tab E2E** — enable memory on an agent, submit and complete a task, navigate to the Memory tab, confirm entry appears, click detail, confirm fields, delete entry, confirm disappearance.
- **Scenario: Submit with Attached Memories** — open Submit page, select the same agent, open the attach picker, select two entries, submit, open the submitted task's detail, confirm attachment list + preview, wait for completion, confirm new memory entry appears in the Memory tab.

Each scenario lists the URLs, browser actions, and expected outcomes matching the existing scenario format. Include scenario selection conditions (which PRs should run them).

### Fault-injection fixtures

- **Summarizer outage:** a pytest fixture that stubs the summarizer LangChain client to raise on every call. Assert `summarizer_model_id='template:fallback'`.
- **Embedding outage:** a fixture that stubs `compute_embedding` to return `None`. Assert `content_vec IS NULL` and `memory.embedding.deferred` in structured logs.
- **Both outages at once:** assert a row is still written, with template summary AND `content_vec = NULL`.

### Crash-recovery fixture

- A deterministic injection point (env var `MEMORY_WRITE_CRASH_AFTER_STATE_COMMIT=1`) that causes the worker to `os._exit(1)` immediately after the `memory_write` state update but before the DB commit.
- Test: submit task → worker reaches crash point → supervisor restarts worker → reaper re-claims → task completes → single memory row exists.

### Budget-carve-out fixture

- A custom `models` row with a high-per-token cost for the summarizer model.
- A `budget_max_per_task` set below the expected summarizer cost but above regular-step costs.
- Submit a long task with enough regular steps to stay below the cap until the `memory_write` super-step.
- Assert: the task status reaches `completed`, NOT `paused`. The hourly-spend ledger shows the memory-write cost recorded.

### Archival + status update

On successful completion of every test:

```
git mv docs/exec-plans/active/phase-2/track-5 docs/exec-plans/completed/phase-2/
```

Then edit `STATUS.md` to flip Track 5 to "Done" with the relevant summary line (match the style of existing track entries).

## Acceptance Criteria

- [ ] A PR-description table maps each of the 15 design-doc acceptance criteria to at least one executed test; every row links to a passing test.
- [ ] `make test-all` passes. `make e2e-test` passes. `make worker-test` passes.
- [ ] Playwright Scenario 1 + Memory-tab scenario + Submit-attach scenario all pass end-to-end against `make start`.
- [ ] Crash-recovery fixture demonstrates exactly one memory row after reaper re-claim.
- [ ] Budget-carve-out fixture demonstrates a memory-enabled task completes despite the `memory_write` cost exceeding `budget_max_per_task`.
- [ ] Cross-tenant / cross-agent assertions across every memory-touching endpoint return a uniform 404 shape.
- [ ] `max_entries` FIFO trim fires on INSERT branch; UPDATE branch leaves row count unchanged. Trim count matches `memory.write.trim_evicted` log.
- [ ] The active directory is moved to `completed/` and `STATUS.md` is updated.
- [ ] `progress.md` in the new `completed/` location reflects all tasks as "Done".

## Testing Requirements

- **Coverage:** the AC-mapping table is the primary deliverable of this task.
- **Determinism:** every test uses deterministic seeds / fixtures; no flaky LLM-dependent assertions. Where the summarizer output is used in assertions, mock the summarizer to return a fixed value.
- **Performance:** the suite must not regress test wall-clock time by more than ~20% over the current baseline. If a test is slow, document why in a comment and gate it behind an E2E-only marker.
- **Cleanup:** integration tests use the isolated test DB on port 55433. No test writes to the dev DB on 55432. Enforce via `E2E_DB_DSN` plumbing per the existing conventions.

## Constraints and Guardrails

- Do not mock the worker or the API service in E2E — exercise the real components. Mocks are permitted only at provider boundaries (summarizer LLM, embedding provider) and at clearly labeled fault-injection points.
- Do not add retries to tests; they should pass deterministically. Flakes indicate real bugs.
- Do not leave the directory in `active/` after Track 5 completion — the archival move is part of the task.
- Do not modify the design doc (unless you find a contract inconsistency discovered during testing — in that case, update the design doc in a SEPARATE PR and cross-link).
- Do not skip the Playwright scenarios — AGENTS.md §Browser Verification makes them blocking.

## Assumptions

- Tasks 1–10 have shipped and are behaviourally correct.
- The `make start` stack is in known-good shape and can run the full Console + API + worker.
- The test DB on port 55433 supports pgvector (Task 1 delivered this).
- The existing fault-injection conventions (env vars, fixtures) in the worker and API suites are extensible.

<!-- AGENT_TASK_END: task-11-integration-and-browser-tests.md -->
