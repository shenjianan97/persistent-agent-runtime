<!-- AGENT_TASK_START: task-10-console-context-management-form.md -->

# Task 10 — Console: Agent Edit Form "Context Management" Section

## Agent Instructions

**CRITICAL PRE-WORK:**
1. `docs/design-docs/phase-2/track-7-context-window-management.md` — section "Agent config extension".
2. `services/console/src/features/agents/` — the agent edit form structure. Find the existing memory config section (Track 5) and mirror the pattern.
3. `docs/CONSOLE_BROWSER_TESTING.md` — current scenarios and the pattern for adding a new one.
4. Track 5 Task 9 (`task-9-console-memory-tab.md`) — precedent for per-agent config Console UI.
5. `services/console/src/features/agents/memory/` — the memory sub-section. Context-management will live alongside it in a similar shape.

**CRITICAL POST-WORK:**
1. Run `make console-test` — React unit tests.
2. Orchestrator runs Playwright verification per AGENTS.md §Browser Verification. Subagent DOES NOT run `make start` or Playwright MCP tools.
3. Update Task 10 status in `docs/exec-plans/active/phase-2/track-7/progress.md`.

## Context

The Agent edit form grows a new "Context management" section exposing the four fields from Task 1:

- `enabled` (toggle, default true)
- `summarizer_model` (model dropdown, optional)
- `exclude_tools` (chip-input list, max 50 entries)
- `pre_tier3_memory_flush` (toggle, only actionable when memory is enabled)

The section reuses styling and validation patterns from Track 5's memory section to keep agent-config UX coherent.

## Task-Specific Shared Contract

- New section component lives at `services/console/src/features/agents/ContextManagementSection.tsx`.
- Section renders inside the agent edit form after the memory section.
- Field IDs (stable test IDs for Playwright):
  - `data-testid="context-management-enabled"` — Switch/Toggle
  - `data-testid="context-management-summarizer-model"` — Select
  - `data-testid="context-management-exclude-tools"` — Tag input (react-select or similar)
  - `data-testid="context-management-pre-tier3-flush"` — Switch/Toggle
- The summarizer_model dropdown is populated from the existing `models` API (same helper Track 5's summarizer model dropdown uses).
- `pre_tier3_memory_flush` toggle is visually disabled (not hidden) when `memory.enabled=false`, with a tooltip "Requires memory to be enabled."
- Form submission sends the four fields nested under `agent_config.context_management`; when `enabled=true` but no other fields set, the payload is `{ context_management: { enabled: true } }`.
- Existing agents without a `context_management` sub-object render with `enabled=true` as the displayed default (because that's the runtime default); the form does NOT automatically POST defaults back on save — it sends the sub-object only when the user actually modified any field. If the user explicitly flips `enabled` and then back, the sub-object is still sent (with the last-set values).
- The chip input caps at 50 entries; on the 51st attempt, show inline error "Maximum 50 entries".
- Unknown tool names in `exclude_tools` are accepted silently (matches Task 1's validation).

## Affected Component

- **Service/Module:** Console — Agents
- **File paths:**
  - `services/console/src/features/agents/ContextManagementSection.tsx` (new)
  - `services/console/src/features/agents/AgentConfigForm.tsx` (modify — include the new section)
  - `services/console/src/features/agents/types.ts` (modify — extend `AgentConfig` type with `context_management`)
  - `services/console/src/features/agents/__tests__/ContextManagementSection.test.tsx` (new)
  - `docs/CONSOLE_BROWSER_TESTING.md` (modify — add "Scenario 14: Context Management section" describing the orchestrator's Playwright verification steps)
- **Change type:** new component + form integration + type extension + unit tests + new scenario

## Dependencies

- **Must complete first:** Task 1 (API accepts the sub-object).
- **Parallel-safe with:** Tasks 2–9 (different code areas).
- **Provides output to:** Task 11 (scenarios run by orchestrator).

## Implementation Specification

### `ContextManagementSection.tsx`

Mirror `MemoryConfigSection.tsx` (or whatever Track 5 named it). Receives current config + change handler:

```tsx
interface Props {
  value: ContextManagementConfig | undefined;
  memoryEnabled: boolean;                          // from the parent form's memory section state
  availableSummarizerModels: ModelSummary[];
  onChange: (next: ContextManagementConfig) => void;
  errors?: FieldErrors;
}
```

Render fields in order: `enabled` toggle → `summarizer_model` dropdown → `exclude_tools` chip input → `pre_tier3_memory_flush` toggle (disabled when `!memoryEnabled`).

`ContextManagementConfig` type:

```ts
export interface ContextManagementConfig {
  enabled?: boolean;
  summarizer_model?: string;
  exclude_tools?: string[];
  pre_tier3_memory_flush?: boolean;
}
```

### `AgentConfigForm.tsx` integration

Import `ContextManagementSection`, place it after the memory section, pipe through `agentConfig.context_management` state + change handler. Include in the form's overall save payload under `context_management`.

### Scenario addition to `CONSOLE_BROWSER_TESTING.md`

Add **Scenario 14: Context Management section**. Covers:

- Create agent → fill all four fields → save → reload → fields persist
- Edit agent → disable `context_management.enabled` → save → reload → reflects disabled
- Invalid `summarizer_model` (pick a disabled row) → save → 400 surfaces inline
- Exclude tools > 50 entries → inline error
- `pre_tier3_memory_flush` toggle is visually disabled when memory is off → enabling memory unblocks it

## Acceptance Criteria

- [ ] `make console-test` — all React unit tests pass, including the new `ContextManagementSection.test.tsx`.
- [ ] The section renders the four fields in the correct order with the correct test IDs.
- [ ] Form submission includes `agent_config.context_management` in the request body when any field is set.
- [ ] Chip input accepts tool name strings and caps at 50 entries with inline error on the 51st.
- [ ] `pre_tier3_memory_flush` toggle is visually disabled (but not hidden) when `memory.enabled=false`, with the tooltip copy.
- [ ] `summarizer_model` dropdown lists models from the existing API helper; disabled models are visible but not selectable (or filtered — match Track 5's behavior).
- [ ] An existing agent with no `context_management` sub-object loads into the form with the `enabled` toggle visually showing true (default) and no sub-object is sent on save if the user makes no change.
- [ ] The section has no console errors on initial render or on field changes.
- [ ] `docs/CONSOLE_BROWSER_TESTING.md` contains Scenario 14 with the five verifications above.

## Testing Requirements

- **Unit tests (`ContextManagementSection.test.tsx`):**
  - Render each field with correct initial value.
  - `onChange` fires with the updated config shape.
  - Tooltip appears when `memoryEnabled=false`.
  - Chip-input cap at 50.
  - Valid submission payload shape.
- **No live Playwright run in this task** — the subagent produces the scenario manifest; the orchestrator executes the Playwright run per AGENTS.md §Browser Verification.

## Constraints and Guardrails

- Do not add per-task knobs to the Submit page — Track 7 has no per-task overrides in v1.
- Do not add Console UI for platform-owned constants (thresholds, per-result cap, KEEP_TOOL_USES) — those are not user-configurable.
- Do not call Playwright MCP tools or `make start` / `make stop`.
- Do not duplicate Track 5's memory section — context_management is a sibling, not a child.
- Do not inline the SUMMARIZER_PROMPT in the UI; it is platform-owned and does not surface in customer-facing forms.

## Assumptions

- Track 5's Agent edit form structure is live; this task extends it.
- The models API (`GET /v1/models` or similar) is available and used by Track 5's summarizer dropdown; reuse the same helper.
- `AgentConfig` type has been extended on the API client side to include `context_management: ContextManagementConfig`.
- The Console test harness uses React Testing Library + Vitest (or whatever Track 5 added).

<!-- AGENT_TASK_END: task-10-console-context-management-form.md -->
