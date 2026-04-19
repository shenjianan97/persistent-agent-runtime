# Phase 2 Track 7 — Context Window Management: Orchestrator Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep long-running tasks viable by bounding the in-task message-history growth that otherwise pushes tasks into context-limit or cost-limit failure. Deliver a three-tier compaction pipeline (tool-result masking, tool-call argument truncation, retrospective summarization) that runs inside the LangGraph executor loop on a deterministic, cache-stable, monotone transform, plus a per-tool-result byte cap at ingestion and a narrow per-agent opt-out.

**Architecture:** A worker-local `compaction` module (`services/worker-service/executor/compaction/`) exposes pure-function transforms plus an orchestrator that runs before every LLM call inside `agent_node`. A single unified `RuntimeState` TypedDict holds fields from both Track 5 (memory) and Track 7 (compaction); graph topology branches per feature, not state schema (matches LangGraph best practice and avoids the unsupported "swap schema per task" path). State extensions (`cleared_through_turn_index`, `truncated_args_through_turn_index`, `summarized_through_turn_index`, `summary_marker`, `memory_flush_fired_this_task`, `last_super_step_message_count`) use monotone reducers so watermarks only advance — preserving the KV-cache prefix across LLM calls. Per-tool-result byte cap (25KB) lives in the tool-execution wrapper so no oversized ToolMessage ever enters state. Thresholds scale with the model's context window (50% Tier 1, 75% Tier 3, fraction-only — no absolute caps). Track 7 is **always-on** for every agent — no per-agent opt-out; an operator kill switch (`CONTEXT_MGMT_KILL_SWITCH` env var) is the incident escape hatch. Pre-Tier-3 memory flush (one-shot agentic turn to `memory_note`) fires only when `agent.memory.enabled` is also on.

**Tech Stack:** Python + LangGraph + LangChain (worker); Spring Boot / Jackson (API agent config extension); React/TypeScript (Console agent edit form); PostgreSQL enum extension (Track 2 `dead_letter_reason` enum); Langfuse spans (observability).

---

## A1. Implementation Overview

Track 7 extends the Phase 2 worker runtime with:

1. **Agent config extension** — `agent_config.context_management` sub-object with four narrow fields (`enabled`, `summarizer_model`, `exclude_tools`, `pre_tier3_memory_flush`). No DB migration. Jackson-safe, canonicalised, validated.
2. **Compaction constants + threshold resolver** — `compaction/defaults.py` holds platform-owned constants (fractions, `KEEP_TOOL_USES`, per-result cap, truncatable arg keys). `compaction/thresholds.py` exposes `resolve_thresholds(model_context_window)` returning per-model `(tier1, tier3)` tokens.
3. **Per-tool-result ingestion cap** — every `ToolMessage` is head+tail truncated to 25KB before entering state; capped at the tool-execution wrapper inside `graph.py`; emits `compaction.per_result_capped` + Langfuse annotation when fired.
4. **Tier 1 transform — tool-result clearing** — pure function `clear_tool_results()` in `compaction/transforms.py` that replaces older `ToolMessage` content with deterministic placeholders while keeping the most recent `KEEP_TOOL_USES` tool uses intact. Monotone via `cleared_through_turn_index` watermark.
5. **Tier 1.5 transform — tool-call argument truncation** — pure function `truncate_tool_call_args()` in the same module that rewrites large string args (`content`, `new_string`, `old_string`, `text`, `body`) in older `AIMessage.tool_calls[*]` records. Monotone via `truncated_args_through_turn_index` watermark.
6. **Tier 3 summarizer** — `compaction/summarizer.py` wraps the summarizer LLM call with retry, structured prompt, cost-ledger attribution (`operation='compaction.tier3'`), and Langfuse span. Returns append-only content that merges into `summary_marker`.
7. **State schema + pipeline orchestrator + `agent_node` integration** — `compaction/state.py` defines `CompactionEnabledState` with `max`/append reducers. `compaction/pipeline.py` runs tiers in order (Tier 1 + 1.5 always; Tier 3 only above threshold) and returns the compacted view + watermark advances. `executor/graph.py` wires the pipeline into `agent_node` and picks the right state class when Track 5 is also on.
8. **Pre-Tier-3 memory flush** — before the first Tier 3 firing per task, when `agent.memory.enabled AND context_management.pre_tier3_memory_flush AND NOT memory_flush_fired_this_task`, the pipeline inserts a one-shot `SystemMessage` asking the agent to call `memory_note`. Heartbeat/recovery turns skip the flush.
9. **Dead-letter reason + budget carve-out** — adds `context_exceeded_irrecoverable` to the Track 2 `dead_letter_reason` enum (schema migration) and wires it from the pipeline's hard-floor path. Adds `compaction.tier3` to the Track 3 named-node budget carve-out list (alongside `memory_write`).
10. **Console UI** — extension to the Agent edit form adding a "Context management" section with the four narrow override fields. Mirrors Track 5 Memory tab's edit patterns. Browser-verified.
11. **Integration + browser tests** — E2E acceptance-criteria coverage, cache-stability regression tests, kill-switch pass-through parity tests, Playwright scenarios for Console + Langfuse event emission.

**Canonical design contract:** `docs/design-docs/phase-2/track-7-context-window-management.md` — the spec agents must read before implementing any task.

---

## A2. Impacted Components

| Component | Path | Change Type | Description |
|-----------|------|-------------|-------------|
| Agent config (API) | `services/api-service/.../model/request/ContextManagementConfigRequest.java`, `AgentConfigRequest.java`, `service/ConfigValidationHelper.java`, `service/AgentService.java` | new + modification | Nested `context_management` sub-object; Jackson mapping; validation of `summarizer_model` against `models`; exclude_tools cap at 50; canonicalisation round-trip |
| Compaction constants + thresholds | `services/worker-service/executor/compaction/defaults.py`, `compaction/thresholds.py`, `compaction/__init__.py` | new code | Platform constants, per-model threshold resolver, type exports |
| Per-tool-result cap | `services/worker-service/executor/compaction/caps.py`, `executor/graph.py` (tool wrappers) | new code + modification | Byte-level head+tail cap; wired into every tool-execution wrapper; structured log event |
| Tier 1 transform | `services/worker-service/executor/compaction/transforms.py` | new code | `clear_tool_results()` pure function with monotone watermark |
| Tier 1.5 transform | same module | new code | `truncate_tool_call_args()` pure function with monotone watermark |
| Tier 3 summarizer | `services/worker-service/executor/compaction/summarizer.py` | new code | LLM call wrapper + retry + cost ledger + Langfuse span |
| State schema | `services/worker-service/executor/compaction/state.py` | new code | `CompactionEnabledState` TypedDict with `max` reducers for watermarks + custom reducer for `summary_marker` |
| Pipeline orchestrator | `services/worker-service/executor/compaction/pipeline.py` | new code | `compact_for_llm(state, messages, config) -> (messages, watermarks, events)` |
| `agent_node` integration | `services/worker-service/executor/graph.py` | modification | Call pipeline before every LLM invocation; merge state class with `MemoryEnabledState` when Track 5 also on; pick plain `MessagesState` when both off |
| Pre-Tier-3 memory flush | `services/worker-service/executor/compaction/pipeline.py` (extension) | modification | Detect trigger, check gating flags, insert one-shot `SystemMessage`, set `memory_flush_fired_this_task` |
| Dead-letter reason | `infrastructure/database/migrations/0014_context_exceeded_dead_letter_reason.sql`, `services/api-service/.../enums/DeadLetterReason.java`, `services/worker-service/core/worker.py` | new migration + modification | Enum addition for `context_exceeded_irrecoverable`; pipeline hard-floor path transitions via the existing dead-letter API |
| Budget carve-out | `services/worker-service/executor/graph.py` or wherever the Track 3 named-node carve-out list lives | modification | Add `compaction.tier3` to the named-node carve-out alongside `memory_write` |
| Console edit form | `services/console/src/features/agents/ContextManagementSection.tsx`, `features/agents/AgentConfigForm.tsx` | new + modification | Section with the four override fields; summarizer_model dropdown sourced from `models`; exclude_tools chip-input; Playwright scenario addition |
| Integration / E2E tests | `services/worker-service/tests/test_compaction_*.py`, `tests/backend-integration/test_context_management_*.py`, Playwright scenarios in `CONSOLE_BROWSER_TESTING.md` | new + modification | 15-AC coverage + cache-stability regression + parity tests + browser verification |

---

## A3. Dependency Graph

```
Task 1 (API Agent Config Extension — Java) ──────────────────────────────┐
                                                                          │
Task 2 (State Schema Unification — Worker refactor) ────────┬────────────┤
  (BLOCKS every subsequent worker task; all Track 5 tests   │            │
   must pass before Track 7 features are added)             │            │
                                                            ▼            │
                  Task 3 (Constants + Thresholds) ──┬──► Task 4 (Per-Result Cap) ───┐
                                                    │                                │
                                                    ├──► Task 5 (Tier 1 Transform) ─┤
                                                    │                                │
                                                    ├──► Task 6 (Tier 1.5 Transform)┤
                                                    │                                │
                                                    └──► Task 7 (Tier 3 Summarizer) ┤
                                                                                     │
               Tasks 3 + 4 + 5 + 6 + 7 ──► Task 8 (Pipeline + agent_node wiring)
                                                                                     │
                                                            Task 8 ──► Task 9 (Pre-Tier-3 Flush)
                                                                                     │
Task 10 (Dead-Letter Reason Enum) ──────────────────────────────────────────────────┤
                                                                                     │
Task 1 ──► Task 11 (Console Edit Form) ─────────────────────────────────────────────┤
                                                                                     │
                                                    Tasks 1..11 ──► Task 12 (E2E + Browser)
```

**Parallelisation opportunities:**

- **Task 1 and Task 2 can run in parallel** — Task 1 is Java API surface, Task 2 is Python worker refactor; zero file overlap.
- **Task 2 is a hard blocker for every other worker-side task.** The state-schema refactor must land, all existing Track 5 tests must pass, and the commit must be green before Tasks 3–9 begin. This isolates refactor failures from feature failures.
- **Tasks 4, 5, 6, 7 can run in parallel** after Task 3 completes — they all add distinct new files under `services/worker-service/executor/compaction/`. **`compaction/__init__.py` is owned by Task 8** — Tasks 2–7 leave it as a minimal docstring-only file; Task 8 fills in the full public API. This avoids the shared-file conflict that would otherwise require worktree isolation for every parallel task.
- **Task 8 is the integration step** — must wait for Tasks 3–7. Touches `services/worker-service/executor/graph.py` (swap `MemoryEnabledState` references for `RuntimeState`, add pipeline call site) and `executor/compaction/__init__.py`. Adds `pipeline.py`, `tokens.py`. Task 2 already did the state-schema refactor, so Task 8 ONLY adds Track 7 fields to `RuntimeState` and the `compact_for_llm` call — smaller than if Task 8 owned the schema work too.
- **Task 9 must serialize after Task 8** — extends the pipeline added in Task 8.
- **Task 10 can run in parallel with Tasks 3–9** — migration + enum addition in a different area, no overlap with compaction module.
- **Task 11 can run in parallel with Tasks 3–10** — Console TypeScript, no overlap with worker Python or migration. Depends on Task 1 for the API contract.
- **Task 12 depends on everything.**

Follow **AGENTS.md §Parallel Subagent Safety** — Tasks 5 and 6 both add to `compaction/transforms.py`; if parallelised, dispatch at least one under `isolation: "worktree"` so the Edit tool does not clobber. Task 8 touches `executor/graph.py` heavily; any subagent touching that file in parallel must use worktree isolation.

---

## A4. Data / API / Schema Changes

**No new tables.** Track 7 is a worker-local transform; all new durable state lives in existing LangGraph checkpoint blobs (JSONB, already schema-compatible) and `agents.agent_config` (JSONB, extended with one sub-object).

**Enum extension:** `dead_letter_reason` gains the value `context_exceeded_irrecoverable`. Picked up by a new migration (`0014_context_exceeded_dead_letter_reason.sql`). The enum write is additive and non-breaking for existing rows.

**`agents.agent_config`:** Additive JSONB sub-object `context_management { summarizer_model, exclude_tools, pre_tier3_memory_flush }` — three tuning fields, no `enabled` key. Absent sub-object uses platform defaults; requests with an `enabled` field are rejected 400.

**`POST /v1/agents` + `PUT /v1/agents/{agent_id}`:** Accepts `context_management` in the request body; validates per §A2 rules; persists verbatim (no silent defaults written).

**No task-submission payload changes.** Track 7 has no per-task knobs in v1.

**No new REST endpoints.** All exposure is via the existing agent CRUD.

**Cost ledger:** existing `agent_cost_ledger` used for Tier 3 summarizer calls, tagged `operation='compaction.tier3'`. Schema unchanged.

---

## A4.1. Task Handoff Outputs

| Task | Output |
|------|--------|
| Task 1 | `agent_config.context_management` accepted (three fields — no `enabled`), validated, canonicalised verbatim; `summarizer_model` cross-checked against `models`; Jackson round-trips the sub-object; requests with `enabled` key rejected 400 |
| Task 2 | **Pure refactor, zero behavior change.** Worker's graph state unified to a single `RuntimeState` TypedDict (Track 5 fields only at this stage). `MemoryEnabledState if stack_enabled else MessagesState` branching in `_build_graph` replaced with `state_type = RuntimeState`. All existing Track 5 tests pass. |
| Task 3 | `compaction/defaults.py` + `compaction/thresholds.py` — pure, unit-tested, no dependencies on LangGraph internals. `PLATFORM_EXCLUDE_TOOLS` includes all five: `memory_note`, `save_memory`, `request_human_input`, `memory_search`, `task_history_get`. |
| Task 4 | `compaction/caps.py` with `cap_tool_result()`; tool wrappers always apply the cap unless `CONTEXT_MGMT_KILL_SWITCH=true` (operator-only escape hatch); `compaction.per_result_capped` emitted when the cap fires |
| Task 5 | `compaction/transforms.py::clear_tool_results` — pure, deterministic, monotone; cache-stability unit test passes |
| Task 6 | `compaction/transforms.py::truncate_tool_call_args` — pure, deterministic, monotone; cache-stability unit test passes |
| Task 7 | `compaction/summarizer.py::summarize_slice` — retry + cost ledger + Langfuse span; handles summarizer outage with structured retries |
| Task 8 | Track 7 fields added to `RuntimeState` (already unified in Task 2) — watermarks, summary marker with strict-append reducer, `last_super_step_message_count`. `compaction/pipeline.py::compact_for_llm`; `compaction/tokens.py::estimate_tokens` (real tokenizer for Anthropic/OpenAI, heuristic for others); `compaction/__init__.py` final public-API surface; `agent_node` invokes pipeline before every LLM call (skip if kill switch on); `compaction.tier3` added to the Track 3 named-node budget carve-out |
| Task 9 | Pipeline extension: one-shot pre-Tier-3 memory flush; respects `memory.enabled`, `pre_tier3_memory_flush`, heartbeat/recovery detection, `memory_flush_fired_this_task` one-shot flag |
| Task 10 | Migration `0014_context_exceeded_dead_letter_reason.sql` (DROP+ADD CHECK constraint pattern per `0010_sandbox_support.sql`); enum value plumbed through Java + Python; pipeline hard-floor path transitions tasks via the existing dead-letter API |
| Task 11 | Console Agent edit form "Context management" section with `summarizer_model`, `exclude_tools`, `pre_tier3_memory_flush` (no `enabled` toggle); Playwright scenario added |
| Task 12 | 15-AC E2E coverage manifest + cache-stability regression tests + kill-switch pass-through tests + Playwright scenarios for Console + observability event emission |

---

## A5. Integration Points

| Caller | Callee | Interface Change | Failure Handling |
|--------|--------|-------------------|-----------------|
| API — `AgentController` | `ConfigValidationHelper.validateContextManagementConfig` | Validates `summarizer_model`, `exclude_tools` size ≤ 50, field bounds | Validation error → 400 with per-field message consistent with Track 5 style |
| `agent_node` | `compaction.pipeline.compact_for_llm` | Pure transform returning compacted messages + watermark advances + events | Exception raised in pipeline → propagates to caller → existing retry / dead-letter path catches it |
| Tool wrapper (in `graph.py`) | `compaction.caps.cap_tool_result` | Applies byte cap before constructing `ToolMessage` | Never raises — cap is always safe to apply |
| `compact_for_llm` | `compaction.transforms.clear_tool_results` | Pure state transform | No failure mode — deterministic |
| `compact_for_llm` | `compaction.transforms.truncate_tool_call_args` | Pure state transform | No failure mode — deterministic |
| `compact_for_llm` | `compaction.summarizer.summarize_slice` | LLM call with retry | Failure after retries → `compaction.tier3_skipped` event; watermark not advanced; next call re-attempts |
| `summarize_slice` | `agent_cost_ledger` (via existing repository) | One row per call, `operation='compaction.tier3'` | Ledger write failure propagates — consistent with other cost-ledger write sites |
| `summarize_slice` | summarizer LLM (LangChain client) | Cheap-model chat completion | Provider error retried per `SUMMARIZER_MAX_RETRIES` |
| `compact_for_llm` (Task 8) | Track 5's memory tool surface | Inserts `SystemMessage` referencing `memory_note` | Tool registration is Track 5's contract; Track 7 assumes it's registered when `memory.enabled=true` |
| Pipeline hard-floor path | Worker dead-letter API | Transitions task with `dead_letter_reason='context_exceeded_irrecoverable'` | Same error-surfacing path as other dead-letter transitions |
| Console | Agent CRUD API | New fields in edit form | Validation errors shown inline |

---

## A6. Deployment and Rollout

Same single-deployment pattern as Tracks 1–5. Key sequencing:

1. **Migration `0014_context_exceeded_dead_letter_reason.sql`** ships with Task 9. CHECK-constraint DROP+re-ADD pattern (see Task 9 for exact shape — `dead_letter_reason` is a TEXT+CHECK column, not a Postgres enum). **Must land before Task 7 code** — otherwise a hard-floor transition produces a CHECK-violation and the reaper can't dead-letter a stuck task.
2. **Traditional deploy + watch (not a canary-via-config rollout).** Track 7 is always-on for all agents; there is no per-agent toggle. Ship to staging, verify metrics, ship to prod. No Week-1/Week-2 config flips.
3. **Kill switch.** `CONTEXT_MGMT_KILL_SWITCH=true` on a worker process forces Track 7 off for that worker's task runs. Worker-restart only; no DB change, no deploy. Runbook-documented escape hatch. In-flight tasks using Track 7 state lose it when the worker re-builds the graph on resume (compaction fields stay at last-persisted values but pipeline is a no-op); Tier 3 re-fires on their next run at extra cost — acceptable for an emergency revert.
4. **Rollout watch.** Langfuse metrics tracked continuously:
   - `tier3_fire_rate` — target < 1 per 100 LLM calls. Exceeds → lower `TIER_1_TRIGGER_FRACTION` platform default.
   - `cache_hit_rate_ratio` = post-Track-7 cached-token-fraction / pre-Track-7 cached-token-fraction on the same agents — target within 5%. A drop signals a KV-cache-stability regression (the monotonicity or strict-append invariants have been violated).
   - `compaction.summary_marker_non_append` log count — must be zero in production; non-zero means a reducer regression slipped through.
5. **Regression test gate in CI.** Every compaction unit test runs twice (enabled + disabled) to guard the "disabled = pre-Track-7 behavior" invariant. Break that invariant and CI fails.

---

## A7. Observability

**Structured log events** — emitted with `tenant_id`, `agent_id`, `task_id`, `step_index`:

- `compaction.per_result_capped` — ToolMessage head+tail truncated at ingestion
- `compaction.tier1_applied` — Tier 1 advanced `cleared_through_turn_index`
- `compaction.tier15_applied` — Tier 1.5 advanced `truncated_args_through_turn_index`
- `compaction.tier3_fired` — Tier 3 summarizer ran, with cost + token count
- `compaction.tier3_skipped` — Tier 3 trigger hit but summarizer call failed after retries
- `compaction.memory_flush_fired` — pre-Tier-3 memory flush inserted for the one-shot
- `compaction.context_exceeded_irrecoverable` — hard floor hit; task dead-lettering

**Langfuse spans:**

- `compaction.inline` (one per agent-node call that applied Tier 1 or Tier 1.5) — attributes: `est_tokens_saved`, `watermarks_advanced`
- `compaction.tier3` (one per Tier 3 firing) — wraps the summarizer LLM span; attributes: `summarizer_model`, `turns_summarized`, `cost_microdollars`
- `compaction.memory_flush` (one per firing) — attributes: `triggered_by='pre_tier3'`
- Per-result cap — emits annotation on the parent tool span

**Metrics:** per-agent rolling-hour `tier1_fire_rate`, `tier3_fire_rate`, `avg_tokens_saved_per_call`, `cache_hit_rate_ratio`. Per-task rollup: max watermarks, total `compaction_cost_microdollars`. Dashboards deferred to Phase 3+.

---

## A8. Risks and Open Questions

| Risk | Mitigation |
|------|-----------|
| Tier 3 summary non-determinism breaks cache across retries | Summary marker is checkpointed post-call; on crash during summarizer, the next attempt produces a possibly-different marker — accepted because the marker is not replayed as a decision-making input. Unit test asserts all other compaction output is byte-identical on repeat runs. |
| Watermark regression silently corrupts output | State schema uses `max` reducer so a returning `{cleared_through: 5}` when current is `10` is ignored. Unit test feeds a regressing watermark and asserts current value wins. |
| Per-tool cap byte truncation breaks JSON/structured tool outputs | Head + tail truncation preserves opening + closing braces; a small `[... truncated X bytes ...]` insert is visible in the payload. Tool documentation (Track 8) will note agents should not rely on contiguous middle bytes. BYOT tools that return JSON must tolerate truncation or return smaller payloads. |
| Summarizer outage leaves task above Tier 3 threshold | Tier 1/1.5 continue to fire on every call and usually keep input under the model context limit even without Tier 3. Only when Tier 1/1.5 together cannot does the task hit the hard floor — expected to be rare. |
| `exclude_tools` list grows unbounded and memory usage balloons | Cap at 50 entries (matches `tool_servers`). Validation rejects over-50 at `POST/PUT /v1/agents`. |
| Cache-stability invariant silently broken by future refactor | Unit test runs the compaction pipeline twice on the same state and asserts `output == output`. CI gate. |
| Customer workloads silently regress after Track 7 ships | Metrics watch in staging (synthetic long tasks) and prod (Langfuse dashboards). Worker-process kill switch (`CONTEXT_MGMT_KILL_SWITCH`) is the escape hatch for the platform operator; customers have no opt-out because an agent without compaction fails at the context ceiling anyway. |
| Pre-Tier-3 memory flush fires on heartbeat turn, wasting a memory_note cycle | Detection rule: last two messages both `AIMessage` → heartbeat. Skip flush. Unit tested. |
| Budget carve-out omitted for `compaction.tier3` → Tier 3 pauses mid-compaction | Task 7 explicitly adds `compaction.tier3` to the Track 3 named-node carve-out list alongside `memory_write`. Regression test verifies carve-out by constructing a task with a tight per-task budget + forced Tier 3. |
| Dead-letter reason enum migration ordering races with existing in-flight tasks | Enum additions are additive + non-breaking; in-flight tasks keep working with pre-existing reasons. Migration runs during normal deploy. |
| Track 5 + Track 7 both extend graph state → accidental state field collision | Task 7 merges `CompactionEnabledState` + `MemoryEnabledState` into a single `RuntimeState` TypedDict with distinct field names; unit test instantiates the combined state and asserts all fields present. |
| Tier 3 summary marker grows unbounded across many Tier 3 firings | Append-only marker is still a budget-line item; if summary_marker itself grows past a secondary threshold, pipeline logs a `compaction.marker_oversized` warning (operator-visible), but v1 does not auto-collapse. Addressed in Phase 3+ if metrics warrant. |
| Customer tool relies on old ToolMessage content surviving forever (e.g., a tool_call that references a previous tool_result by content) | `exclude_tools` documented as the opt-out for "load-bearing" tool results; `memory_note`, `save_memory`, `request_human_input` seeded by default. Customer tools can be added. |

---

## A9. Orchestrator Guidance

- Use `docs/design-docs/phase-2/track-7-context-window-management.md` as the canonical design contract. Every task spec must reference the relevant section of that document.
- `CONTEXT_MGMT_KILL_SWITCH=true` on a worker MUST yield zero pipeline invocations, zero new structured log lines, zero new cost ledger rows, and tool-wrapper pass-through (byte-identical). Regression gate: worker unit tests run twice (kill switch off + on) in CI.
- Watermark reducers MUST be `max` (not stock `operator.add` and not raw assignment). Enforce in `state.py` unit tests.
- `summary_marker` MUST NOT be replayed to the summarizer when Tier 3 fires a second time — the summarizer receives only the newly-old slice, and the new summary appends to the existing marker.
- Pipeline transforms MUST be deterministic and monotone. Cache-stability unit test (`output == output` on repeat runs of the same input state) is mandatory in Task 7.
- Per-tool-result cap applies universally, including to BYOT tool results, `memory_search` / `task_history_get` results, and `request_human_input` responses. No tool is exempt.
- Thresholds are **fraction-only** in v1 (no absolute token caps). Customers picking large-context models (1M Gemini) get proportionally higher thresholds. Deferred `aggressive_compaction` override is explicitly out of scope.
- Tier 3 summarizer uses the agent's `context_management.summarizer_model` when set, else the platform default `claude-haiku-4-5` (same pattern as Track 5's `memory.summarizer_model`). Reuse the existing `models`-table validation helper.
- Follow the **silent compaction rule** — placeholder language is neutral ("tool output not retained"), never "you are being compacted." Exception: the pre-Tier-3 memory flush system message (explicitly documented to the agent).
- Pre-Tier-3 memory flush fires **at most once per task**. Heartbeat/recovery turns (last two messages both `AIMessage`) skip the flush. `memory_flush_fired_this_task` flag is monotone.
- Do NOT implement: Anthropic native API primitive usage (`clear_tool_uses_20250919`, `compact_20260112`) — deferred to Phase 3+. Per-task knobs on `POST /v1/tasks`. Learned / RL-trained policies. Cross-task compaction inheritance. `aggressive_compaction` override.
- Follow **AGENTS.md §Parallel Subagent Safety** — Tasks 4/5 both edit `compaction/transforms.py`; Task 7 edits `graph.py` heavily; any parallel agent touching the same file must use `isolation: "worktree"`.

---

## A10. Key Design Decisions

1. **Fraction-only thresholds.** Tier 1 at 50%, Tier 3 at 75% of the model's effective budget. No absolute token caps — customers who pick 1M Gemini get proportionally higher thresholds. Minimum-separation guardrail prevents pathologically small models (8K) from firing both tiers simultaneously.
2. **`enabled=true` by default.** The failure modes (context-limit, TPM, cost) affect every long task, not just opt-in ones. Opt-out is the escape hatch for verbatim-history workloads (< 5% expected).
3. **Platform-owned constants, narrow per-agent overrides.** `keep`, per-result cap, trigger fractions live as module constants. Customer tuning surface is four fields only: `enabled`, `summarizer_model`, `exclude_tools`, `pre_tier3_memory_flush`.
4. **Monotone watermark state with `max` reducers.** `cleared_through_turn_index`, `truncated_args_through_turn_index`, `summarized_through_turn_index` only advance. Cache-stability invariant enforced at the reducer level and unit-tested.
5. **Observation masking (Tier 1) is the primary mechanism, not LLM summarization.** JetBrains paper empirical result: masking beats summarization on cost and solve rate. Tier 3 is last resort.
6. **Silent compaction (no "context anxiety" induction).** Neutral placeholder language; no `SystemMessage` telling the agent it was compacted. Exception: the explicit pre-Tier-3 memory flush.
7. **Per-tool-result cap at ingestion, not in compaction.** 25KB head+tail cap lives in the tool wrapper so no oversized `ToolMessage` ever enters state. Compaction sees already-sane history.
8. **Tier 3 summary marker is append-only.** Subsequent Tier 3 firings append, never rewrite, preserving KV-cache on the marker region.
9. **No DB migration (beyond enum extension).** Agent config is JSONB; all compaction state lives in LangGraph checkpoint blobs (JSONB). Only schema change: `dead_letter_reason` enum gains `context_exceeded_irrecoverable`.
10. **Budget carve-out for `compaction.tier3`.** Matches Track 5's `memory_write` pattern — per-task pause check skipped for the named node; hourly-spend accounting still applies.
11. **Track 5 interaction scoped to one field + one SystemMessage.** `pre_tier3_memory_flush` gates a one-shot agentic turn inserted before Tier 3's first firing per task. Skipped on heartbeat turns; skipped entirely when memory is off.
12. **Server-side Anthropic primitives deferred to Phase 3+.** Client-side compaction for provider portability in v1; layering `clear_tool_uses_20250919` / `compact_20260112` on Anthropic agents is a follow-up optimization.
13. **Phased rollout with worker-level kill switch.** New agents get compaction on in Week 1; existing agents get it in Week 2 conditional on clean Week 1 metrics; `CONTEXT_MGMT_KILL_SWITCH` env var forces global off without a deploy. This replaces the original "default-on for everyone at rollout" plan after reviewer feedback: blast radius of a regression across every active task is too large to absorb in a single step.
14. **Real tokenizer preferred for Anthropic / OpenAI.** Heuristic-only estimation under-counts by 30–50% on code/JSON-heavy history (exactly the case Track 7 exists to address), so `tiktoken` / `anthropic.count_tokens` is mandatory on those providers; Gemini / BYOT use the cheap char-count heuristic until a specific provider proves persistently inaccurate.
15. **`__init__.py` owned by Task 7.** Tasks 2–6 don't touch it, avoiding the parallel-task merge conflict that would otherwise require worktree isolation on every preceding task.

---

## B. Agent Task Files

| Task | File | Description |
|------|------|-------------|
| Task 1 | [task-1-agent-config-extension.md](agent_tasks/task-1-agent-config-extension.md) | `agent_config.context_management` sub-object: Jackson, validation, canonicalisation (API) |
| Task 2 | [task-2-state-schema-unification.md](agent_tasks/task-2-state-schema-unification.md) | **Refactor.** Unified `RuntimeState` TypedDict replaces Track 5's binary schema selection. All existing Track 5 tests pass; zero behavior change. Blocks every subsequent worker-side task. |
| Task 3 | [task-3-compaction-constants-and-thresholds.md](agent_tasks/task-3-compaction-constants-and-thresholds.md) | `compaction/defaults.py` + `compaction/thresholds.py` — platform constants + `resolve_thresholds()` |
| Task 4 | [task-4-per-tool-result-cap.md](agent_tasks/task-4-per-tool-result-cap.md) | `compaction/caps.py` head+tail cap; integration into tool wrappers; kill-switch pass-through |
| Task 5 | [task-5-tier-1-transform.md](agent_tasks/task-5-tier-1-transform.md) | `compaction/transforms.py::clear_tool_results` — pure, deterministic, monotone |
| Task 6 | [task-6-tier-1-5-transform.md](agent_tasks/task-6-tier-1-5-transform.md) | `compaction/transforms.py::truncate_tool_call_args` — pure, deterministic, monotone |
| Task 7 | [task-7-tier-3-summarizer.md](agent_tasks/task-7-tier-3-summarizer.md) | `compaction/summarizer.py::summarize_slice` with retry, cost ledger, Langfuse span |
| Task 8 | [task-8-pipeline-and-graph-integration.md](agent_tasks/task-8-pipeline-and-graph-integration.md) | Track 7 state fields added to `RuntimeState`; pipeline orchestrator; `agent_node` wiring; budget carve-out |
| Task 9 | [task-9-pre-tier3-memory-flush.md](agent_tasks/task-9-pre-tier3-memory-flush.md) | Pipeline extension: one-shot pre-Tier-3 memory flush with positional heartbeat skip |
| Task 10 | [task-10-dead-letter-reason.md](agent_tasks/task-10-dead-letter-reason.md) | Migration + Java enum + Python constant for `context_exceeded_irrecoverable` |
| Task 11 | [task-11-console-context-management-form.md](agent_tasks/task-11-console-context-management-form.md) | Console Agent edit form "Context management" section + Playwright scenario |
| Task 12 | [task-12-integration-and-browser-tests.md](agent_tasks/task-12-integration-and-browser-tests.md) | 15-AC E2E + cache-stability regression + kill-switch pass-through tests + Playwright scenarios |
