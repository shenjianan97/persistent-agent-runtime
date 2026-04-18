<!-- AGENT_TASK_START: task-12-task-memory-mode.md -->

# Task 12 — Task Memory Mode: `memory_mode` enum replaces `skip_memory_write`

## Agent Instructions

You are a software engineer implementing one module of a larger system. Your scope is strictly limited to this task.

**CRITICAL PRE-WORK:** Before beginning implementation, you MUST read:
1. `docs/design-docs/phase-2/track-5-memory.md` — sections "API Surface" (bottom half covering `POST /v1/tasks` extensions) and "Validation and Consistency Rules"; this task extends the submission payload and adds a new validation invariant.
2. `services/worker-service/executor/memory_graph.py` — `MemoryEnabledState` schema (lines 111–127) and the `effective_memory_enabled` gate predicate (lines 135–154). Both are rewritten here.
3. `services/worker-service/executor/graph.py` — the gate read site at line 1289–1292, the `memory_write` node registration at 589–603, the `agent → memory_write/END` routing at 615–619 and 623–624, and the post-commit path at 2002–2030.
4. `services/worker-service/tools/memory_tools.py` — `_build_memory_note_tool` at lines 255–278 (pattern for the new `save_memory` tool) and the registration gate at 597–599.
5. `services/api-service/.../model/request/TaskSubmissionRequest.java` — the `skipMemoryWrite` field being removed.
6. `services/api-service/.../service/ConfigValidationHelper.java` — `validateMemoryConfig` (lines 158–200), where the new cross-field invariant lives.
7. `services/console/src/features/submit/SubmitTaskPage.tsx` — the existing checkbox (line 500–525) being replaced by a dropdown.

**CRITICAL POST-WORK:** After completing this task:
1. Run `make test` and `make e2e-test`. Fix any regressions.
2. Add a new Playwright scenario to `docs/CONSOLE_BROWSER_TESTING.md` per the "Browser Verification" section below.
3. Update the status in `docs/exec-plans/active/phase-2/track-5/progress.md` to reflect Task 12 and append its row to the table.

## Context

Today every memory-enabled task unconditionally writes a memory via the `memory_write` LangGraph node. There is no agent-side gate, so trivial follow-ups ("thanks", "got it") still pay summarizer + embedding costs and crowd retrieval with low-signal entries. The existing `skip_memory_write: bool` per-task flag is binary — either write or skip — with no middle ground that lets the agent judge whether the run was worth remembering.

This task replaces `skip_memory_write` with a three-value enum `memory_mode` on task submission:

| Mode | Meaning |
|---|---|
| `always` *(default)* | Every successful task writes a memory (current `skip_memory_write=false` behavior) |
| `agent_decides` | Memory written only if the agent calls a new `save_memory(reason)` tool during the run |
| `skip` | No memory for this task (current `skip_memory_write=true` behavior) |

When the selected agent has `memory.enabled=false`, the dropdown is forced to `skip` and disabled. No agent-level default field is added — `agent.memory.enabled` remains the master gate.

No data migration for existing `tasks.skip_memory_write` rows is required; the user has explicitly waived backward compatibility for this field.

## Task-Specific Shared Contract

- **Request field:** `memory_mode: "always" | "agent_decides" | "skip"` on the `POST /v1/tasks` JSON payload. Optional; default `always`. Replaces the removed `skip_memory_write` boolean.
- **Column:** `tasks.memory_mode TEXT NOT NULL DEFAULT 'always' CHECK (memory_mode IN ('always','agent_decides','skip'))`. The existing `tasks.skip_memory_write BOOLEAN` column is dropped in the same migration.
- **Validation invariant:** the API must reject `memory_mode ∈ {always, agent_decides}` when the task's agent has `memory.enabled=false`. Error envelope matches the existing task-submission 4xx shape.
- **Worker gate:** the existing single-predicate `effective_memory_enabled(agent_config, skip_memory_write) -> bool` becomes `effective_memory_decision(agent_config, memory_mode) -> MemoryDecision` returning two booleans: `stack_enabled` (observations channel, memory_note, attached-memory preamble, memory_write node registration) and `auto_write` (whether `memory_write` fires unconditionally on the terminal branch).
  - `enabled=False OR mode=skip → (False, False)` — identical to today's memory-disabled path.
  - `enabled=True AND mode=always → (True, True)` — current memory-enabled behavior.
  - `enabled=True AND mode=agent_decides → (True, False)` — memory stack on, but `memory_write` routing gated at runtime.
- **New state field:** `MemoryEnabledState.memory_opt_in: bool` (default `False`, no reducer; last-write-wins). The `save_memory` tool sets it to `True`.
- **Per-run reset:** initial state on every run (first execution AND follow-up/redrive) seeds `memory_opt_in=False` explicitly. The opt-in must be re-earned on each run — a follow-up does not inherit run 1's opt-in.
- **New tool `save_memory(reason: str)`:** Registered only when `stack_enabled=True AND auto_write=False` (i.e., `agent_decides` mode). Returns `Command(update={"memory_opt_in": True, "observations": [f"[save_memory] {reason}"]})`. The reason flows into observations so it is checkpointed as a `ToolMessage`, appears in the task timeline, and feeds the summarizer — matching `memory_note`'s shape for observability.
- **Routing:** in `agent_decides` mode, replace the unconditional edge `agent → memory_write | END` with a conditional edge inspecting pending tool calls first, then `auto_write or state.get("memory_opt_in", False)`.
- **Task detail response:** exposes `memory_mode` so the Console task-detail page can show which mode the task ran under (no new timeline marker — silence is the UX signal in `agent_decides` + no-opt).
- **Dropdown UX (Console):** `<Select>` with testid `memory-mode-select`. Three options: "Always save memory" / "Let agent decide" / "Don't save memory". When the selected agent's `memory.enabled=false`, the value snaps to `skip`, the select is disabled, and helper text reads "This agent has memory disabled".

## Affected Component

- **Service/Module:** API Service (task submission + detail), Worker (memory graph + tools), Console (submit page), Database schema.
- **File paths:**
  - `infrastructure/database/migrations/0012_task_memory_mode.sql` (new)
  - `services/worker-service/executor/memory_graph.py` (modify — gate predicate, state schema)
  - `services/worker-service/executor/graph.py` (modify — read `memory_mode`, conditional routing, per-run state seeding, post-commit gate)
  - `services/worker-service/tools/memory_tools.py` (modify — new `_build_save_memory_tool`, registration gate)
  - `services/api-service/.../model/request/TaskSubmissionRequest.java` (modify — drop `skipMemoryWrite`, add `memoryMode` with pattern/enum validation)
  - `services/api-service/.../service/ConfigValidationHelper.java` (modify — cross-field rejection of `always`/`agent_decides` when agent memory disabled)
  - `services/api-service/.../repository/TaskRepository.java` (modify — `insertTaskFromAgent` swaps boolean for string param and SQL placeholder)
  - `services/api-service/.../model/response/TaskDetailResponse.java` or equivalent (modify — surface `memory_mode`)
  - All other API callers of `skipMemoryWrite` (controller pass-through, service layer, DTO mappers) — grep and migrate
  - `services/console/src/types/index.ts` (modify — replace `skip_memory_write?: boolean` with `memory_mode?: enum`)
  - `services/console/src/features/submit/schema.ts` (modify — Zod enum with default `'always'`)
  - `services/console/src/features/submit/SubmitTaskPage.tsx` (modify — replace checkbox with `<Select>`, disabled-when-memory-off branch)
  - `services/console/src/api/client.ts` (modify — `buildSubmitTaskBody` sends `memory_mode`)
  - Worker tests: `test_memory_graph.py`, `test_memory_tools.py`, `test_memory_write.py`, `test_memory_dead_letter.py`, `test_memory_graph_topology.py`, `test_budget_carve_out_end_to_end.py`, `test_track5_ac_mapping.py` (migrate call sites)
  - API tests: `TaskServiceTest.java`, `TaskControllerTest.java` (migrate)
  - Backend integration: `tests/backend-integration/test_memory_task_submission.py`, `tests/backend-integration/helpers/api_client.py` (migrate)
  - `docs/CONSOLE_BROWSER_TESTING.md` (new scenario)
- **Change type:** modification across three services + new migration + new worker tool

## Dependencies

- **Must complete first:** Tasks 1 (migration), 4 (task submission with `skip_memory_write`), 6 (memory_write node), 7 (tool registration) — all Done.
- **Provides output to:** None — terminal task in Track 5.
- **Shared interfaces/contracts:** The `POST /v1/tasks` payload shape (breaking change: `skip_memory_write` removed, `memory_mode` added); the worker's gate predicate API (`effective_memory_enabled` → `effective_memory_decision`).
- **Parallel-safety:** this task alone edits `memory_graph.py`, `graph.py`, `tools/memory_tools.py`, the API request/validation/repo layer, and the Console submit page. Do NOT parallelise subagents on this task — single-agent implementation only.

## Implementation Specification

### Migration (`infrastructure/database/migrations/0012_task_memory_mode.sql`)

One transaction that:
1. `ALTER TABLE tasks DROP COLUMN skip_memory_write;`
2. `ALTER TABLE tasks ADD COLUMN memory_mode TEXT NOT NULL DEFAULT 'always' CHECK (memory_mode IN ('always','agent_decides','skip'));`
3. Inline comment on the column referencing this task spec and the design doc.

CI's migration glob `[0-9][0-9][0-9][0-9]_*.sql` picks up the file automatically. Existing `skip_memory_write` data is NOT preserved — user waived.

### Worker — gate predicate & state (`memory_graph.py`)

- Replace `effective_memory_enabled(agent_config, skip_memory_write) -> bool` with `effective_memory_decision(agent_config, memory_mode) -> MemoryDecision`. `MemoryDecision` is a small frozen dataclass or NamedTuple with two boolean fields: `stack_enabled`, `auto_write`. Mapping per the Shared Contract above.
- Extend `MemoryEnabledState(MessagesState)` at lines 111–127 with `memory_opt_in: bool`. No reducer; default `False`. Document in the class docstring that the field resets per run.

### Worker — new `save_memory` tool (`tools/memory_tools.py`)

Add `_build_save_memory_tool` alongside `_build_memory_note_tool`. Reuse its `StructuredTool + Pydantic args + Command(update=...)` shape. Signature: `save_memory(reason: str)`. Pydantic arg schema mirrors `MemoryNoteArguments` — single string field, 1–2048 chars, stripped.

Tool returns `Command(update={"memory_opt_in": True, "observations": [f"[save_memory] {reason}"]})`. The observations reducer (`operator.add`) appends the reason line.

Registration gate: `memory_note` whenever `stack_enabled=True`; `save_memory` only when `stack_enabled=True AND auto_write=False`.

### Worker — graph wiring (`executor/graph.py`)

- Replace `skip_memory_write = task_data.get("skip_memory_write", False)` at line 1291 with `memory_mode = task_data.get("memory_mode", "always")`; compute `decision = effective_memory_decision(agent_config, memory_mode)` once.
- Thread `decision` where `memory_enabled_for_task` used to flow: graph-schema selection (570), node registration (602), tool registration gate, post-commit path (2002–2030).
- Register `memory_write` node whenever `decision.stack_enabled=True` (both `always` and `agent_decides`). Edge to `END` unchanged.
- Replace the direct `tools_condition` third-arg routing at 615–619 (has-tools branch) and the direct `add_edge` at 623–624 (no-tools branch) with `add_conditional_edges("agent", route_after_agent, {...})` where `route_after_agent(state)` returns:
  - `"tools"` if the last message has pending tool calls;
  - `MEMORY_WRITE_NODE_NAME` if `decision.auto_write or state.get("memory_opt_in", False)`;
  - `END` otherwise.
- In the initial-state seeding block of `execute_task` (wherever the initial state dict is built before `astream`), explicitly set `memory_opt_in: False`. This is the per-run reset — runs must re-earn the opt-in.
- Post-commit gate at 2002–2030: replace the boolean check with `decision.stack_enabled AND (decision.auto_write OR final_state.values.get("memory_opt_in", False))`. When stack enabled but no opt-in, fall through to the "memory-disabled" branch that completes the task with no memory row.
- Grep and update the remaining `skip_memory_write` references at lines 489 (comment), 563 (comment), 2653 (comment).

### API — TaskSubmissionRequest + validation + repo

- Request record: drop `Boolean skipMemoryWrite`; add `String memoryMode` with `@JsonProperty("memory_mode")` and a pattern/regex annotation restricting to the three allowed values (or a dedicated `MemoryMode` enum type deserialised via a Jackson converter). Default when absent: `"always"`.
- `ConfigValidationHelper.validateMemoryConfig`: add the cross-field rule — reject with the existing task-submission 4xx error shape when `memoryMode ∈ {always, agent_decides}` AND the agent's `memory.enabled=false`. Error message: `"memory_mode cannot be '<value>' because this agent does not have memory enabled"`.
- `TaskRepository.insertTaskFromAgent`: param `boolean skipMemoryWrite` → `String memoryMode`. SQL at lines 70–78 writes `memory_mode` placeholder.
- Grep `skipMemoryWrite` across all `services/api-service/src/main/java/**` and update every caller. Controller pass-through, service layer, DTO mappers — nothing should reference the old name after this task.
- Task detail response DTO: add `memory_mode: string` field so the Console can show which mode the task ran under.

### Console — dropdown UX

- `types/index.ts`: replace `skip_memory_write?: boolean` on `TaskSubmissionRequest` with `memory_mode?: 'always' | 'agent_decides' | 'skip'`. Add `memory_mode: string` to the task-detail response type.
- `features/submit/schema.ts`: Zod enum validator with default `'always'`.
- `features/submit/SubmitTaskPage.tsx`: replace the existing checkbox FormField at lines 500–525 with a `<Select>` using the app's shared Select primitive. testid `memory-mode-select`. Three options with short labels ("Always save memory" / "Let agent decide" / "Don't save memory"). When the selected agent's `memory.enabled=false`, set the value to `skip`, disable the select, and render helper text "This agent has memory disabled". When the agent's memory is enabled, default selection is `always`. Update the form state, reset, and payload-serialization call sites at 53, 185, and 234–235.
- `api/client.ts`: `buildSubmitTaskBody` always includes `memory_mode` (no conditional — simpler than today's checkbox-conditional serialization).
- Task detail page: render `memory_mode` somewhere in the task metadata so users can see which mode the task ran under.

### Observability

No new timeline marker is required for `agent_decides` + no-opt. The absence of a "Memory Saved" marker combined with the task-detail `memory_mode` field communicates the state clearly. In `agent_decides` + opt-in, the existing `save_memory` tool call appears in the timeline as a `ToolMessage` — organic debuggability without new UI code.

## Acceptance Criteria

- [ ] Migration 0012 drops `tasks.skip_memory_write` and adds `tasks.memory_mode` with the CHECK constraint.
- [ ] `POST /v1/tasks` with no `memory_mode` persists `memory_mode='always'`.
- [ ] `POST /v1/tasks` with each of `memory_mode ∈ {always, agent_decides, skip}` persists the correct value.
- [ ] `POST /v1/tasks` with `memory_mode: "invalid"` rejects with 400.
- [ ] `POST /v1/tasks` with `memory_mode='always'` or `memory_mode='agent_decides'` for an agent whose `memory.enabled=false` rejects with the existing task-submission 4xx envelope.
- [ ] `POST /v1/tasks` with `memory_mode='skip'` for any agent (including memory-disabled) succeeds.
- [ ] Task detail response surfaces `memory_mode`.
- [ ] Worker with `memory_mode='skip'` exercises no memory stack — no `memory_note`, no `save_memory`, no `memory_write`, no attached-memory preamble, no observation seeding. Identical behaviour to today's `memory.enabled=false`.
- [ ] Worker with `memory_mode='always'` writes a memory on successful terminal branch — identical behaviour to today's `skip_memory_write=false`.
- [ ] Worker with `memory_mode='agent_decides'` registers `memory_note` AND `save_memory` tools; does NOT register `save_memory` in the other two modes.
- [ ] Worker with `memory_mode='agent_decides'` and no `save_memory` call completes the task with NO `agent_memory_entries` row and NO dead-letter memory write.
- [ ] Worker with `memory_mode='agent_decides'` and `save_memory("reason X")` called by the agent writes a memory row; the reason appears as an observation in the task timeline.
- [ ] Follow-up: run 1 with `agent_decides` opts in and writes memory; run 2 with same mode does NOT opt in → run 2 writes no second memory (per-run reset confirmed).
- [ ] Console Submit page shows a `<Select>` with three options. Default is `always`. Disabled + locked to `skip` when selected agent has memory disabled, with helper text "This agent has memory disabled".
- [ ] Console task detail page shows `memory_mode` for the task.
- [ ] All tests pass; no reference to `skip_memory_write` / `skipMemoryWrite` remains in the codebase outside migration history files.

## Testing Requirements

### Worker — unit tests (`test_memory_graph.py`, `test_memory_tools.py`)

- Truth table for `effective_memory_decision`: all combinations of `(memory.enabled ∈ {true,false}) × (mode ∈ {always, agent_decides, skip})`, asserting both `stack_enabled` and `auto_write`.
- `MemoryEnabledState` includes `memory_opt_in` (default `False` when unset).
- `save_memory` tool: returns `Command` with `memory_opt_in=True` and the reason appended as `[save_memory] <reason>` observation.
- Tool registration gate: `save_memory` registered ONLY in `agent_decides`; `memory_note` registered in both `always` and `agent_decides`; neither in `skip`.

### Worker — topology tests (`test_memory_graph_topology.py`)

- `skip` mode: no `memory_write` node, no conditional routing to it.
- `always` mode: direct routing agent → memory_write on terminal branch (regression).
- `agent_decides` mode: conditional edge function exists; resolves to `memory_write` when `state.memory_opt_in=True`; resolves to `END` when `memory_opt_in=False`.

### Worker — integration tests (new `test_agent_decides_commit.py` or extend `test_memory_write.py`)

- Run graph end-to-end with a stubbed agent that (a) NEVER calls `save_memory` under `agent_decides` → assert no `pending_memory` in final state, no row in `agent_memory_entries`, no dead-letter invocation.
- Same but (b) agent calls `save_memory("because X")` → assert `memory_opt_in=True` in final state, memory row written, reason visible in observations snapshot, summarizer cost ledger row present.
- Follow-up regression: run 1 opts in, completes; run 2 does NOT opt in → run 2's post-commit path skips memory write (per-run reset).

### Worker — dead-letter regression (`test_memory_dead_letter.py`)

- Dead-letter memory template does NOT fire under `agent_decides` + no-opt.
- Dead-letter memory template DOES fire under `agent_decides` + opt-in + simulated memory_write failure (unchanged from today).

### API — unit + controller tests

- Accept each of three valid values.
- Reject invalid strings with 400.
- Reject `always`/`agent_decides` for agent with `memory.enabled=false`.
- Persist the correct value via `TaskRepository`.
- Task detail GET includes `memory_mode`.

### Backend integration (`tests/backend-integration/test_memory_task_submission.py`)

- Three end-to-end submissions, one per mode, asserting `tasks.memory_mode` value after insert and correct post-run state in `agent_memory_entries`.
- Cross-agent validation: submission with `memory_mode='always'` against a memory-disabled agent returns the expected 4xx.

### Console — unit tests

- Dropdown renders three options, defaults to `always`.
- Dropdown forces `skip` and disables when selected agent has `memory.enabled=false`.
- Submit request body contains `memory_mode`, not `skip_memory_write`.

### Browser Verification (add to `docs/CONSOLE_BROWSER_TESTING.md`)

New scenario "Task Memory Mode Dropdown":

Preconditions: two agents seeded — `agent-memory-on` (`memory.enabled=true`) and `agent-memory-off` (`memory.enabled=false`).

1. Navigate to Submit Task. Select `agent-memory-on`. Assert `memory-mode-select` exists, defaults to "Always save memory", and is enabled.
2. Change to "Let agent decide". Submit a task whose prompt instructs the agent to call `save_memory(reason="test reason")`. After completion, navigate to the Memories page and assert a new row appears. Open task detail; confirm `memory_mode` shows `agent_decides` and the timeline includes the `save_memory` tool call with the reason.
3. Repeat with a prompt that does NOT call `save_memory`. Assert no memory row is created, no "Memory Saved" marker appears on the timeline, and `memory_mode=agent_decides` on task detail.
4. Change to "Don't save memory". Submit a task. Assert no memory row, no memory-related timeline entries, and task detail shows `memory_mode=skip`.
5. Select `agent-memory-off`. Assert dropdown snaps to "Don't save memory" and is disabled with helper text "This agent has memory disabled". Submission succeeds and persists `memory_mode=skip`.
6. Attempt a crafted `POST /v1/tasks` (via devtools) for `agent-memory-off` with `memory_mode: "always"`. Assert 400 with validation error referencing the memory-enabled invariant.

## Constraints and Guardrails

- Do NOT add an agent-level default field for `memory_mode`. The existing `agent_config.memory.enabled` is the master gate; everything else is per-task.
- Do NOT preserve the `skip_memory_write` column or keep a compatibility read path in the worker. Breaking change by user decree.
- Do NOT introduce an append-only reducer for `memory_opt_in`. Simple overwrite semantics match the feature: save_memory writes True, initial state writes False.
- Do NOT persist the `save_memory` reason into `agent_memory_entries`. It lands in the observations snapshot only; the existing memory row schema is unchanged.
- Do NOT add a new Console timeline marker for `agent_decides` + no-opt. Absence of the "Memory Saved" marker combined with the task-detail `memory_mode` field communicates the state.
- Do NOT expand the memory-write budget carve-out to `save_memory` invocations — the tool itself is zero-rated; only the summarizer + embedding costs still flow through the existing carve-out.
- Do NOT attempt in-flight migration of running tasks. Only newly-submitted tasks after migration 0012 use the new column.

## Assumptions

- Tasks 1, 4, 6, 7, 8 are Done (see `progress.md`).
- The CI workflow picks up new migrations automatically via the `[0-9][0-9][0-9][0-9]_*.sql` glob.
- The Console's shared `<Select>` component supports a `disabled` prop and helper-text rendering (verify in `services/console/src/components/ui/`; fall back to the existing agent picker's pattern if needed).
- No change to the existing agent-level `agent_config.memory` sub-object shape.
- The `save_memory` reason is free-form; length bounds match `memory_note` (1–2048 chars).
- Running this task's migration against a database that holds in-flight tasks is acceptable — those tasks default to `memory_mode='always'` via the ALTER DEFAULT.

<!-- AGENT_TASK_END: task-12-task-memory-mode.md -->
