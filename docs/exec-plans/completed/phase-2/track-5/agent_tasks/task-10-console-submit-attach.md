<!-- AGENT_TASK_START: task-10-console-submit-attach.md -->

# Task 10 — Console Submit-Page Memory Attachment Widget

## Agent Instructions

You are a software engineer implementing one module of a larger system. Your scope is strictly limited to this task.

**CRITICAL PRE-WORK:** Before beginning implementation, you MUST read:
1. `docs/design-docs/phase-2/track-5-memory.md` — section "Console UX → Submit page extension".
2. `services/console/src/features/submit/*` — existing submit-page components.
3. Task 4 output — the `POST /v1/tasks` payload shape extensions (`attached_memory_ids`, `skip_memory_write`).
4. Task 9 output — deep-link contract from the Memory tab's "Attach to new task" button: `/submit?agentId=:id&attachMemoryId=:memory_id`.
5. `docs/CONSOLE_BROWSER_TESTING.md` — existing Submit-page scenarios. This task's flow must be added and exercised.

**CRITICAL POST-WORK:** After completing this task:
1. Run `make test` (Console unit tests). Fix any regressions.
2. **BLOCKING:** Run Playwright Scenario 1 (Navigation Smoke Test) AND the Submit-attach scenario (to be added to `CONSOLE_BROWSER_TESTING.md`). Verify end-to-end per AGENTS.md §Browser Verification.
3. Update the status in `docs/exec-plans/active/phase-2/track-5/progress.md` to "Done".

## Context

Customers can attach past memory entries to a new task at submission. This task delivers the Submit-page UI — a multi-select picker visible only when the selected agent has `memory.enabled=true`, a token-footprint indicator that warns (informationally — does not block) when the total prefix size approaches 10 KB, and wiring of the selection into the `POST /v1/tasks` payload.

## Task-Specific Shared Contract

- **Visibility gate:** the attach widget renders only when the selected agent's `memory.enabled = true`. For memory-disabled agents, the widget is hidden (not greyed out — removed from the DOM). This matches the design doc: attachment is meaningless if the agent cannot use the attached content (though technically the worker would still inject it — this is a UX choice for clarity).
- **Picker contract:**
  - Multi-select over the agent's memory entries.
  - Search / filter by title + outcome + date (delegates to `/v1/agents/:id/memory/search?q=…` for non-empty query, falls back to `/memory` list when empty).
  - Each list item shows: title, outcome badge, created_at, a short summary preview (≤ 100 chars).
  - Selection cap: 50 entries (matches Task 4's server-side cap).
- **Selected list display:** below the picker, show the selected entries inline in `position` order. Each has a remove button.
- **Token-footprint indicator:**
  - Compute approximate prefix bytes = sum over selected entries of `title.length + summary.length + sum(observations.length) + 50` (50 is the per-entry formatting overhead).
  - Render as plaintext: "Attached context: ~3.1 KB · 4 entries".
  - When approx ≥ 10 KB, show in amber with a tooltip: "Large attachment context may increase cost and risk hitting context-window limits".
  - The indicator is INFORMATIONAL — never blocks submission.
- **Deep-link pre-selection:**
  - Parse `?attachMemoryId=:id` on mount. If present and the agent matches, pre-select that entry (fetch its detail to populate the inline display).
  - If the query-param is present but the agent's `memory.enabled = false`, show a toast "Attachment ignored — memory is disabled for this agent" and proceed without attachment.
- **Submission payload:**
  - When selected list is non-empty, include `attached_memory_ids: [id, id, …]` in the POST body (in selection order).
  - When `skip_memory_write` is toggled on, include `skip_memory_write: true` in the body.
- **`skip_memory_write` toggle:**
  - Render as a checkbox below the attach widget: "Skip writing a memory entry for this task (per-task privacy opt-out)".
  - Visible only when the selected agent has `memory.enabled = true` (on disabled agents, there's no memory write anyway).
  - Default: unchecked.
- **Error handling:** If POST returns the Task-4 uniform 4xx "one or more attached_memory_ids could not be resolved", show a toast and leave the form populated so the user can remove the offender. Do NOT name the offending id (the server doesn't tell us — matches the 404-not-403 rule).

## Affected Component

- **Service/Module:** Console — Submit page
- **File paths:**
  - `services/console/src/features/submit/AttachMemoryPicker.tsx` (new — picker + search + selected list)
  - `services/console/src/features/submit/TokenFootprintIndicator.tsx` (new — computed indicator)
  - `services/console/src/features/submit/SubmitTaskPage.tsx` (modify — wire the widget + skip_memory_write + deep-link parsing + payload extension)
  - **Parallel-safety:** Task 9 also edits Console. If dispatched concurrently, use `isolation: "worktree"` on one of them and merge on completion per AGENTS.md §Parallel Subagent Safety.
  - **Plan-added attachment cap:** the 50-entry selection cap below is NOT a design-doc requirement; it mirrors Task 4's server-side cap as defence-in-depth.
  - The Submit feature does NOT use a shared `hooks.ts` — current files are per-hook: `useModels.ts`, `useSubmitTask.ts`. For memory hooks, import directly from `features/agents/memory/hooks.ts` (created in Task 9) rather than adding a local hooks file.
  - `services/console/src/features/submit/*.test.tsx` (new or extend — coverage for the picker, indicator, deep link, payload shape)
- **Change type:** new code + modification

## Dependencies

- **Must complete first:** Task 3 (Memory REST API the picker lists from), Task 4 (task submission payload extensions on the API side).
- **Provides output to:** Task 11 (E2E).
- **Shared interfaces/contracts:** URL query-param `?attachMemoryId=:id` + POST body extensions.

## Implementation Specification

### `AttachMemoryPicker` contract

- Renders a search input, a scrollable list of memory entries for the selected agent, and a "Selected" panel below.
- Internally uses `useAgentMemoryList(agentId, filters, cursor)` and `useAgentMemorySearch(agentId, query, filters)` from Task 9.
- Selection state is controlled by the parent `SubmitPage` so the payload wiring remains simple. Expose `value: string[]` + `onChange: (ids: string[]) => void`.
- De-dupe selections.
- Keyboard a11y: up/down arrows move focus within the list; Enter toggles selection; Esc clears search.

### `TokenFootprintIndicator` contract

- Pure component: accepts `entries: [{title, summary, observations}]` (current selection, resolved via a lightweight cache of `useAgentMemoryDetail` calls — OR via a batched fetch if the API supports one; in v1, do the per-entry detail fetch lazily and cache on selection).
- Computes and renders the approximation as above.

### Deep-link handling

- On SubmitPage mount, parse the URL. If `attachMemoryId` is present:
  - Fetch the entry detail (scoped to the current `agentId`).
  - On success: pre-select it.
  - On 404: show a toast "Memory entry not found for this agent" and proceed without selection.
- On agent change (if the user switches agents in the form), clear the selection.

### Submit payload

Extend the existing submit payload with `attached_memory_ids` (omitted when empty) and `skip_memory_write` (omitted when false — server default is `false`).

### Browser verification checkpoints (during development)

- Navigate to the Submit page.
- Select a memory-enabled agent; the attach widget appears.
- Search for a memory entry; select two; inline list shows both with remove buttons.
- Token-footprint indicator updates.
- Submit the task. Verify via the API or the Console task detail that `attached_memory_ids` is persisted and `task_attached_memories` rows exist.
- From the Memory tab (Task 9), click "Attach to new task" on an entry → SubmitPage opens with that entry pre-selected.
- Toggle `skip_memory_write` → submit → verify worker does not write a memory entry on completion (this requires the worker side to be Task-6/7/8-complete).

## Acceptance Criteria

- [ ] The attach widget is present on the Submit page only when the selected agent has `memory.enabled=true`.
- [ ] The widget lists memory entries for the selected agent with title, outcome, date, short preview.
- [ ] Search filters the picker via `/memory/search`; clearing search falls back to `/memory`.
- [ ] Multi-select works; selected entries render inline in selection order with remove buttons; selection cap enforced at 50.
- [ ] The token-footprint indicator renders a count and approximate KB value, and turns amber at ≥ 10 KB.
- [ ] `attached_memory_ids` is included in the POST payload in selection order, only when the selection is non-empty.
- [ ] Deep link `?attachMemoryId=:id` pre-selects the entry when the target agent has memory enabled.
- [ ] Deep link with a mismatched agent → toast + unselected.
- [ ] `skip_memory_write` checkbox appears only for memory-enabled agents; value is included in the POST payload when checked.
- [ ] API rejection (uniform 4xx) surfaces as a toast without naming the offending id; form is not reset.
- [ ] Component tests cover the picker, the indicator, the deep-link, the skip_memory_write toggle, and the POST payload shape.
- [ ] Playwright browser verification passes the Submit-attach scenario end-to-end.

## Testing Requirements

- **Component tests** (Vitest + React Testing Library + MSW):
  - Widget hidden for memory-disabled agents.
  - Search input switches to search endpoint when non-empty.
  - Selection cap enforcement.
  - Token-footprint calculation correct for known inputs; amber threshold at 10 KB.
  - Deep-link pre-selection + mismatched-agent toast path.
  - `skip_memory_write` reaches the POST payload.
- **Playwright (browser-verified):** add the "Submit with attached memory" scenario to `CONSOLE_BROWSER_TESTING.md` covering agent-selection → picker → select entries → submit → verify on the task detail. Task 11 will flesh out the full-stack assertions, but the browser-verification checkpoints here must pass.

## Constraints and Guardrails

- Do not implement server-side validation of the count cap — the API already caps at 50 (Task 4). The UI cap is defensive, not authoritative.
- Do not block submission on the token-footprint indicator — it is advisory only.
- Do not persist the `skip_memory_write` preference on the user / agent. It is strictly per-submission.
- Do not fetch memory entries for memory-disabled agents — the widget is hidden, so no picker state is live.
- Do not call `/memory` or `/memory/search` at page load; lazy-load when the widget opens.
- Do not leak memory ids across agents — when the agent selection changes, clear the picker state.
- Do not add UI for viewing the resolved prompt-prefix the worker will inject — that detail is internal.
- Do not block the picker fetch on the entire agent list; use cursor pagination and load on scroll or "Load more".

## Assumptions

- The existing Submit page has a controlled form state where the agent id is already tracked — this task plugs into it.
- The Memory API endpoints have reasonable p95 latency (< 500 ms) at typical entry counts; lazy loading + cursor pagination keeps the picker responsive.
- The Console auth layer propagates the tenant context into every memory API call.
- The browser toast utility and confirmation dialog patterns already exist — reuse, do not invent.
- The URL router handles the `?attachMemoryId` query param without further configuration (standard SPA behaviour).

<!-- AGENT_TASK_END: task-10-console-submit-attach.md -->
