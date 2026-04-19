<!-- AGENT_TASK_START: task-12-integration-and-browser-tests.md -->

# Task 12 — Integration + Browser Tests

## Agent Instructions

**CRITICAL PRE-WORK:**
1. `docs/design-docs/phase-2/track-7-context-window-management.md` — section "Acceptance criteria" (14 ACs); every AC must be covered by a test or have a documented reason it isn't.
2. `services/worker-service/tests/test_track5_ac_mapping.py` — precedent for an AC-to-test mapping manifest (Track 5 Task 11).
3. `docs/CONSOLE_BROWSER_TESTING.md` — scenario format + orchestrator verification workflow.
4. `tests/backend-integration/` — existing REST E2E helpers, especially `helpers/api_client.py`.
5. All prior Task 8 outputs — compaction module API, state schemas, event types.

**CRITICAL POST-WORK:**
1. Run `make worker-test`, `make e2e-test`, `make test`. All suites must be green.
2. Orchestrator runs Playwright scenarios (Subagent does not).
3. Update Task 12 status AND flip this track's entry in `STATUS.md` to "Done" once orchestrator Playwright verification confirms.
4. Move `docs/exec-plans/active/phase-2/track-7/` → `docs/exec-plans/completed/phase-2/track-7/`.

## Context

Track 7's acceptance criteria span worker, API, Console, DB, observability. Task 12 is the verification + manifest task — wires the 14 ACs to concrete tests, adds missing coverage, and lands the Playwright scenarios the orchestrator needs.

## Task-Specific Shared Contract

- **AC mapping manifest** at `services/worker-service/tests/test_track7_ac_mapping.py` — one failing-when-missing test per AC, matching Track 5's `test_track5_ac_mapping.py` pattern. Each test asserts the existence of the concrete test file + test function covering the AC; fails with a descriptive error if the target moves.
- **New worker tests** under `services/worker-service/tests/test_compaction_*.py` for ACs 1–11 (see AC list below).
- **New REST E2E tests** under `tests/backend-integration/test_context_management_*.py` for AC 12 (validation).
- **New Playwright scenarios** in `docs/CONSOLE_BROWSER_TESTING.md` (Scenario 14 from Task 11 covers the edit form; a new Scenario 15 covers the Langfuse trace verification for a compaction task — orchestrator executes).

## Affected Component

- **Service/Module:** Tests (worker, backend-integration, Console manifest)
- **File paths:**
  - `services/worker-service/tests/test_track7_ac_mapping.py` (new)
  - `services/worker-service/tests/test_compaction_cache_stability.py` (new — AC 5)
  - `services/worker-service/tests/test_compaction_exclude_tools.py` (new — AC 6)
  - `services/worker-service/tests/test_compaction_summary_marker_append.py` (new — AC 8)
  - `services/worker-service/tests/test_compaction_cost_ledger.py` (new — AC 9)
  - `services/worker-service/tests/test_compaction_budget_carve_out.py` (new — AC 10)
  - `services/worker-service/tests/test_compaction_memory_disabled_no_flush.py` (new — AC 13)
  - `services/worker-service/tests/test_compaction_observability.py` (new — AC 14 automated log-event assertions)
  - `services/worker-service/tests/test_compaction_pre_tier3_flush_redrive.py` (new — AC 7 redrive-safety)
  - `tests/backend-integration/test_context_management_validation.py` (new — AC 12)
  - `docs/CONSOLE_BROWSER_TESTING.md` (modify — add Scenario 15)
  - `docs/exec-plans/active/phase-2/track-7/progress.md` (modify — mark Task 12 done; update STATUS.md; move directory)
- **Change type:** new tests + manifest + scenario addition + completion bookkeeping

## Dependencies

- **Must complete first:** Tasks 1–10.
- **Provides output to:** STATUS.md flip; orchestrator Playwright verification.

## Implementation Specification

### AC ↔ Test coverage

Track 7 design doc lists 14 ACs; Task 12 ensures each is covered:

| AC # | Covered by |
|------|-----------|
| 1 — tier1 fires on long tasks | `test_graph_compaction_integration.py::test_tier1_fires_above_threshold` (Task 8) |
| 2 — per-tool cap applied at ingestion | `test_graph_tool_cap_integration.py::test_500k_tool_result_capped` (Task 4) |
| 3 — tier ordering (Tier 3 only after Tier 1/1.5) | `test_compaction_pipeline.py::test_tier3_only_when_tier1_insufficient` (Task 8) |
| 4 — watermark reducer rejects regressions | `test_compaction_state_reducers.py::test_max_reducer_rejects_regression` (Task 8) |
| 5 — cache-stability invariant | **NEW** `test_compaction_cache_stability.py` (this task) |
| 6 — exclude_tools never masked | **NEW** `test_compaction_exclude_tools.py` (this task) |
| 7 — pre-Tier-3 flush once per task + heartbeat skip + survives redrive | `test_compaction_pre_tier3_flush.py` (Task 9) plus a new redrive-safety E2E test in `test_compaction_pre_tier3_flush_redrive.py` (this task) that saves a post-flush checkpoint, redrives, and asserts the flag is restored (no second flush) |
| 8 — summary_marker append-only | **NEW** `test_compaction_summary_marker_append.py` (this task) |
| 9 — Tier 3 cost ledger attribution | **NEW** `test_compaction_cost_ledger.py` (this task) |
| 10 — budget carve-out for compaction.tier3 | **NEW** `test_compaction_budget_carve_out.py` (this task) |
| 11 — context_exceeded_irrecoverable dead-letter | `test_compaction_hard_floor.py` (Task 10) |
| 12 — API validation of context_management fields | **NEW** `test_context_management_validation.py` (this task, REST E2E) |
| 13 — memory-disabled never fires flush | **NEW** `test_compaction_memory_disabled_no_flush.py` (this task) |
| 14 — Langfuse spans present | **Automated:** `test_compaction_observability.py` asserts `compaction.inline` / `compaction.tier3` / `compaction.per_result_capped` / `compaction.memory_flush_fired` structured-log events fire at the expected points (mocked LLM + log-capture fixture). **Manual:** Orchestrator Playwright Scenario 15 confirms the Langfuse UI shows the spans (visual verification that the log events translate to trace spans correctly). |

### `test_track7_ac_mapping.py`

Mirror `test_track5_ac_mapping.py`. Each of the 14 ACs gets one meta-test that asserts the concrete test file + function exists. Failing this test tells future maintainers which AC has drifted from its coverage.

### `test_compaction_cache_stability.py` (AC 5)

Construct a realistic state with a long messages history and non-None summary_marker. Run `compact_for_llm` twice with the same inputs (same mock summarizer that returns the same text). Assert:

- `result_a.messages == result_b.messages` (deep equality)
- `result_a.state_updates == result_b.state_updates`
- Byte-level identical placeholder strings

### `test_compaction_exclude_tools.py` (AC 6)

Construct a task history with 20 tool calls interleaved between `memory_note` and `web_search`. Force Tier 1 to fire. Assert every `memory_note` `ToolMessage.content` is unchanged; every old `web_search` `ToolMessage.content` is replaced with the placeholder.

### `test_compaction_summary_marker_append.py` (AC 8)

Mock summarizer returning distinct text on each call. Force Tier 3 to fire twice in the same task. Assert:

- `state.summary_marker` after first call = summary_1
- `state.summary_marker` after second call = summary_1 + "\n\n..." + summary_2 (the append shape)
- `summarized_through_turn_index` advanced on both calls

### `test_compaction_cost_ledger.py` (AC 9)

Force one Tier 3 firing with a mocked summarizer returning a real response (with mocked response_metadata for token counts). Read back from `agent_cost_ledger`. Assert exactly one row with `operation='compaction.tier3'`, correct `task_id`, `agent_id`, `tenant_id`, non-zero `tokens_in` and `tokens_out`, `cost_microdollars` matching the formula against the summarizer model's pricing.

### `test_compaction_budget_carve_out.py` (AC 10)

Construct an agent with `budget_max_per_task = 100_000_000` microdollars (tight). Force a single Tier 3 firing whose summarizer cost would push the task over the per-task budget. Assert:

- The task does NOT transition to `waiting_for_budget`.
- The task continues normally after the compaction call.
- The hourly-spend rollup still reflects the Tier 3 cost.

### `test_compaction_memory_disabled_no_flush.py` (AC 13)

Agent with `memory.enabled=false` and `context_management.pre_tier3_memory_flush=true`. Force Tier 3. Assert:

- `compaction.memory_flush_fired` NOT logged.
- No SystemMessage with `additional_kwargs.compaction_event == "pre_tier3_memory_flush"` in the compacted messages.
- Tier 3 fires normally.

### `test_context_management_validation.py` (AC 12, REST E2E)

Using the live API against the E2E DB (per `CLAUDE.md` — port 55433):

- POST `/v1/agents` with valid `context_management` → 201.
- POST with `summarizer_model="does-not-exist"` → 400.
- POST with `exclude_tools` of size 51 → 400.
- POST with `pre_tier3_memory_flush=true` AND `memory.enabled=false` → 201 (no cross-field validation).

### Scenario 15 in `CONSOLE_BROWSER_TESTING.md`

Covers: Orchestrator runs a task long enough to cross Tier 1 threshold → inspects Langfuse (via the existing trace-URL on the task detail page) → confirms `compaction.inline` span present on ≥ 1 LLM call, `compaction.per_result_capped` annotation present if any tool returned > 25KB. Pass/fail checklist for orchestrator.

## Acceptance Criteria

- [ ] `services/worker-service/tests/test_track7_ac_mapping.py` lists all 14 Track 7 ACs and each references an existing test — all pass.
- [ ] All eight new worker test files exist and pass under `make worker-test`.
- [ ] `tests/backend-integration/test_context_management_validation.py` passes under `make e2e-test`.
- [ ] `docs/CONSOLE_BROWSER_TESTING.md` contains Scenarios 14 + 15 with clear orchestrator-runnable steps.
- [ ] `make test`, `make worker-test`, `make e2e-test` — all green.
- [ ] `docs/exec-plans/active/phase-2/track-7/progress.md` shows all 12 tasks as Done.
- [ ] `STATUS.md` row for Track 7 flipped from "Not started" to "Complete" with links to plan + progress.
- [ ] The `docs/exec-plans/active/phase-2/track-7/` directory has been moved to `docs/exec-plans/completed/phase-2/track-7/`. **The orchestrator (not this subagent) performs the move** after Playwright verification passes — see AGENTS.md §Browser Verification and Task 12 CRITICAL POST-WORK. The subagent marks Task 12 internal status Done but does NOT move the directory.

## Testing Requirements

- Every test listed above is new in this task unless marked as owned by an earlier task.
- Tests must not require live LLM credentials. Mock all summarizer calls.
- Tests must not require live Langfuse credentials. For the one Langfuse-span test (AC 14), the assertion is Playwright-inspection by the orchestrator, not automated.
- REST E2E tests use the `par-e2e-postgres` DB on port 55433 per `CLAUDE.md`.

## Constraints and Guardrails

- Do not run Playwright MCP tools or `make start` / `make stop` as a subagent. Orchestrator owns browser execution (AGENTS.md §Browser Verification).
- Do not modify any Task 1–10 source to make tests pass. If a test reveals a defect, open a follow-up for the owning task.
- Do not move the directory to `completed/` until Playwright scenarios pass. The orchestrator does that step after verification.
- Do not skip the AC mapping manifest — it is what makes AC coverage auditable.

## Assumptions

- Track 5 Task 11's `test_track5_ac_mapping.py` pattern is acceptable and will be extended as-is.
- `make e2e-test` brings up the isolated test DB (port 55433) and applies migrations automatically.
- The Playwright orchestrator workflow documented in `CONSOLE_BROWSER_TESTING.md` is the source of truth for how scenarios are run.

<!-- AGENT_TASK_END: task-12-integration-and-browser-tests.md -->
