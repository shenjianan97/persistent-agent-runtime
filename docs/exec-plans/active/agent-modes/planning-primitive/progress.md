# Agent Modes — Planning Primitive — Progress

Status tracking for the Planning Primitive track ([plan.md](./plan.md)).
Sibling track: [Supervisor Topology](../supervisor-topology/progress.md).
Statuses: `Not started` · `In progress` · `Done` · `Blocked`.

| Task | Description | Status | Note |
|---|---|---|---|
| P1 | `RuntimeState.plan` (full-replace reducer) + `plan_write` tool + v1 caps | Done | spec + quality reviewed (approved 2026-06-06); 43 unit tests; review fixes: required `items`, `PlanItem` schema wired, typed errors, duplicate-id rejection |
| P2 | Post-compaction plan injection as a neutral Markdown system block | Done | spec + quality reviewed (approved 2026-06-06); 29 unit tests. Two evidence-verified deviations from the spec's literal wording, both adjudicated justified: (1) the block is a tagged `HumanMessage` — `langchain_anthropic` 1.3.4 raises on non-consecutive `SystemMessage`s (same wall + same fix as the Track-7 memory-flush block); (2) `prompt_cache/anthropic.py`+`bedrock.py` skip plan-tagged messages in their breakpoint scans — pre-change the tail was unconditionally marked, so no uncached suffix existed and a changing plan would have busted the sliding cache every turn. Registry-parametrized invariant test guards future strategies. |
| P3 | `GET /v1/tasks/{id}/plan` read-only projection | Done | spec + quality reviewed (approved 2026-06-06); 12 new Java tests; added scope delivered: `plan_write` in `ValidationConstants.ALLOWED_TOOLS`; refactor: checkpoint-payload parsing deduped into `JsonParseUtil.parseJsonMap` (Activity + Plan projections share it); note for P4: `updated_at` is omitted-when-null (`updated_at?: string`), badge renderer must tolerate null/unknown `status` |
| P4 | Console plan checklist + `api.getTaskPlan` fetch hook | Done | spec + quality reviewed (approved 2026-06-06); 14 unit tests (153/153 console suite); polls 5s while task non-terminal; null-tolerant badges; Scenario 20 authored — browser run pending (orchestrator gate) |
| P5 | Planning E2E + Playwright scenario | Done | spec + quality reviewed (approved 2026-06-06); 11 worker integration tests (real-graph Tier-3 compaction survival) + 6 backend-integration tests on the isolated harness; **surfaced + fixed (user-approved added scope) an activation defect:** `canonicalizeConfig` silently stripped `plan_write` — now preserved via `ValidationConstants.OPT_IN_TOOLS` (create + update paths, 4 new Java tests); plan-block size measured: 11,233 chars / 1,839 cl100k tokens worst-case, ceilings asserted (follow-up #2 dispositioned); browser scenarios pending orchestrator run |

## Notes

- This track is independent of the Supervisor track and carries **no open blockers** — it can ship first.
- Shared-file coordination (`state.py`, `_get_tools`, `ActivityPane.tsx`, `types/index.ts`) with the Supervisor track: worktree-isolate if both run concurrently (plan §A3).
- E9 (plan-injection KV-cache position) is resolved in this track's P2 (inject in the uncached suffix; assert position, not just unchanged-plan byte-identity). P2 had to create the uncached suffix: the strategies previously always marked the tail, so they now skip plan-tagged messages in both breakpoint scans.

## Named follow-ups (from review; track is archivable with these recorded, not silently dropped)

1. **Dedupe the breakpoint-scan logic between `prompt_cache/anthropic.py` and `bedrock.py`** (P2 quality review, 2026-06-06). The two `apply_cache_markers` bodies are structurally identical 20-line blocks that now carry the load-bearing plan-skip invariant; a future edit to one can drift. Fix shape: template method on the parent class with a `_mark_message` hook. Both copies are independently tested today; the registry-parametrized invariant test catches behavioral drift, so this is hygiene, not risk.
2. **P5 must report the injected plan block's observed token size.** — **DISPOSITIONED (2026-06-06): Closed with evidence.** Measured worst-case (50 items × 200-char titles): 11,233 chars ≈ 1,839 cl100k tokens (<1% of a 200K window); typical plans ~100–400 tokens. `test_plan_block_size_measurement_for_max_plan` asserts ceilings (11,500 chars / 3,000 tokens) so a rendering change reopens this row.
3. **P4 renderer contract notes (from P3 review):** `updated_at` is omitted-when-null (`updated_at?: string`, not `string | null`); the status badge must tolerate null/unknown `status` values (a corrupted checkpoint can yield null-field items; the API deliberately projects them tolerantly rather than 500ing). — **Delivered in P4** (null-tolerant badges + optional typing, unit-tested).
4. **API-wide 405 hygiene (P5 finding, 2026-06-06):** `GlobalExceptionHandler` has no `HttpRequestMethodNotSupportedException` mapping, so unmapped verbs (e.g. `PATCH /v1/tasks/{id}/plan`) return 500 instead of 405 — pre-existing, API-wide, not Planning-specific. Follow-up: add the handler mapping → 405; then tighten `test_plan_read_api.py`'s mutation-verb accepted set from `(404, 405, 500)` to `405`.
