<!-- AGENT_TASK_START: task-9-console-memory-tab.md -->

# Task 9 — Console Memory Tab on Agent Detail

## Agent Instructions

You are a software engineer implementing one module of a larger system. Your scope is strictly limited to this task.

**CRITICAL PRE-WORK:** Before beginning implementation, you MUST read:
1. `docs/design-docs/phase-2/track-5-memory.md` — sections "Console UX → Memory tab on Agent detail page" and "Memory entry detail view".
2. `services/console/src/features/agents/AgentDetailPage.tsx` — the existing route + tab layout. The Memory tab is a new tab alongside existing ones.
3. `services/console/src/features/tool-servers/` — Track 4 Console precedent for a scoped list/detail/delete flow. Use the same conventions (React Query hooks, shadcn/ui components, toast on failure, confirmation dialog on delete).
4. `docs/CONSOLE_BROWSER_TESTING.md` — existing scenarios and the scenario-selection matrix. Task 11 will add the Memory scenarios; this task must use the same testing conventions for browser-verification checkpoints during development.
5. Task 3's output — the Memory REST API shapes the Console consumes.
6. Task 2's output — the agent config exposes `memory.enabled`. When the agent has memory disabled, the tab can still render (historical entries from when it was enabled are browsable and deletable) but the "Attach to new task" shortcut and anything implying "new observations will be captured" must be muted.

**CRITICAL POST-WORK:** After completing this task:
1. Run `make test` (Console unit tests). Fix any regressions.
2. **BLOCKING:** Run the Playwright Scenario 1 (Navigation Smoke Test) AND the Memory-tab feature scenario (which you will add — see "Testing Requirements" below and update `CONSOLE_BROWSER_TESTING.md` accordingly). Verify the full flow works in a real browser per AGENTS.md §Browser Verification.
3. Update the status in `docs/exec-plans/active/phase-2/track-5/progress.md` to "Done".

## Context

Customers need a Console surface to browse, search, read, and delete memory entries. This tab also surfaces storage stats and an 80%-of-cap warning so customers know when they're about to hit FIFO trim.

Route: `/agents/:agentId/memory` — a new tab on the existing Agent detail page.

The design doc specifies the surface end-to-end. This task's job is to translate the spec into:

- A list page with pagination, outcome filter, date-range filter, free-text search field (delegates to `/memory/search`), per-row outcome badge, task link, and per-row delete button.
- A storage-stats summary strip at the top: entry count + approximate bytes. When count ≥ 80% of `max_entries`, a warning banner appears with the current count, the cap, and a link to delete entries.
- A detail view (opens on entry click) showing title, outcome badge, `created_at` + `updated_at`, summary, observations list, tags, linked task (deep-link to task detail), summarizer model id, and an "Attach to new task" shortcut that navigates to the submit page with the memory pre-attached (Task 10 handles the Submit side).
- Deletion with a confirmation dialog; on confirm, DELETE → toast → refetch list.

## Task-Specific Shared Contract

- **Route:** `/agents/:agentId/memory`. A new tab entry in `AgentDetailPage.tsx`.
- **Data layer:** React Query hooks. Names mirror existing patterns (e.g., `useAgentMemoryList`, `useAgentMemoryDetail`, `useAgentMemorySearch`, `useDeleteAgentMemoryEntry`).
- **Filters:** outcome (dropdown: All / Succeeded / Failed), date range (two date inputs → `from` / `to` query params), free-text search (when non-empty, switches the list to `/memory/search?q=…&mode=hybrid`). Empty search field falls back to the list endpoint.
- **Pagination:** cursor-based, "Load more" button or infinite scroll — match whichever pattern the existing task list uses.
- **Outcome badge:** green "Succeeded", red "Failed". Any `summarizer_model_id='template:fallback'` / `'template:dead_letter'` entries should render a subtle tooltip indicating the summary is a template.
- **Storage stats strip:** entry count (e.g., "4,217 of 10,000 entries") + approx bytes humanised (e.g., "12.3 MB"). At ≥ 80% of the cap, the strip becomes a warning banner. Cap value comes from the agent's `agent_config.memory.max_entries` (or the platform default — make this visible to the Console via an agent-detail API if not already).
- **Delete flow:** confirmation dialog with the entry title and a "this cannot be undone" notice; on confirm, DELETE the entry; on success, show a toast and refetch the list.
- **Entry detail:** inline (slide-over or modal) OR a nested route `/agents/:agentId/memory/:memoryId` — match existing Console precedent. Detail shows all fields listed in "Context" above.
- **"Attach to new task" shortcut:** deep-links to `/submit?agentId=:id&attachMemoryId=:memory_id` (Task 10 picks up this URL parameter).
- **Empty states:** "No memory entries yet" (when the agent has never run a memory-enabled task) vs. "No results match your filters" (when filters narrow away everything) are distinct.
- **Memory-disabled agent behavior:** the tab still renders historical entries when `memory.enabled=false`. Show a dismissible notice at the top: "Memory is disabled for this agent. Existing entries are preserved; no new entries will be written." The "Attach to new task" button is still enabled (attachments don't require memory to be enabled — Task 10's Submit widget is gated by the TARGET agent's `memory.enabled`, not the current browse context).
- **Scope:** all API calls use the current route's `:agentId`. The tenant is implicit (session). Any 404 from the API surfaces as a toast; the list simply empties.

## Affected Component

- **Service/Module:** Console — Agents / Memory
- **File paths:**
  - `services/console/src/features/agents/memory/MemoryTab.tsx` (new — list view + filters + storage stats strip + warning banner)
  - `services/console/src/features/agents/memory/MemoryEntryDetail.tsx` (new — detail view)
  - `services/console/src/features/agents/memory/DeleteEntryDialog.tsx` (new — confirmation dialog)
  - `services/console/src/features/agents/memory/hooks.ts` (new — React Query hooks)
  - `services/console/src/features/agents/memory/api.ts` (new — REST clients for the four endpoints)
  - `services/console/src/features/agents/AgentDetailPage.tsx` (modify — add Memory tab entry + route)
  - `services/console/src/features/agents/memory/*.test.tsx` (new — component tests)
  - `docs/CONSOLE_BROWSER_TESTING.md` (modify — add Memory-tab scenarios; do this in Task 11 or at minimum stub the scenarios here if Task 11 lands later)
- **Change type:** new feature area + modification of AgentDetailPage

## Dependencies

- **Must complete first:** Task 3 (Memory REST API), Task 2 (agent-detail API must surface `memory.enabled` + `max_entries` for the warning-threshold banner and "memory disabled" notice).
- **Provides output to:** Task 10 (the "Attach to new task" shortcut deep-links into the Submit page), Task 11 (E2E / Playwright scenarios).
- **Shared interfaces/contracts:** The Memory API response shapes (from Task 3).
- **Parallel-safety:** Task 10 also edits Console files. If dispatched concurrently, use `isolation: "worktree"` on one of them per AGENTS.md §Parallel Subagent Safety.

## Implementation Specification

### Component contract

- `MemoryTab` is the top-level component for `/agents/:agentId/memory`:
  - Renders the storage-stats strip at the top (fed by the list endpoint's first-page `agent_storage_stats`).
  - Renders the filter bar: outcome dropdown + date range + search input.
  - Renders the list of entries. Each row: title, outcome badge, `created_at`, task link, delete button.
  - On row click → open `MemoryEntryDetail`.
- `MemoryEntryDetail` reads the full entry and shows all fields (title, outcome, summary, observations, tags, linked task, dates, summarizer model id) + an "Attach to new task" button + a "Delete" action.
- `DeleteEntryDialog` — confirmation modal; calls the delete mutation on confirm.

### React Query hooks contract

- `useAgentMemoryList(agentId, filters, cursor)` — list endpoint. Returns items, next cursor, stats (first page only).
- `useAgentMemorySearch(agentId, query, filters)` — search endpoint. Enabled only when `query` is non-empty.
- `useAgentMemoryDetail(agentId, memoryId)` — single-entry GET.
- `useDeleteAgentMemoryEntry(agentId)` — mutation; on success, invalidates list query.

### Storage-stats strip + warning banner

- Below 80% of cap: plain summary — "4,217 of 10,000 entries · ~12.3 MB".
- At ≥ 80%: warning banner with an icon, same counts, plus a "Delete old entries" button (links to a pre-filtered list sorted oldest-first — simplest: flip the list sort order or add a `?sort=created_at_asc` query param if the API supports it; else filter-by-date).
- At the platform max (100,000): banner is red; the message reads "Maximum reached. FIFO trim is removing the oldest entries automatically." **(Plan-added enhancement; the design doc specifies only the 80% warning. The red banner is informational parity with the worker-side FIFO trim that's already active at this point.)**

### Filter + search integration

- Outcome + date filters pass through to both `/memory` and `/memory/search` query params.
- When the search input is non-empty, the view switches from list to search results. Pagination is disabled in search mode (search returns up to `limit=20`). Make this explicit in the UI ("Top 20 matches" label).
- Clearing the search input restores the list view.

### Routing inside the tab

Use a nested route `/:memoryId` under the tab so entry detail is shareable / back-button-friendly. Alternatively a query param `?memoryId=…` — match whatever convention the Track 4 Tool Server detail uses.

### Browser verification checkpoints (during development)

This task MUST be browser-verified before commit. Run the full stack locally and exercise:

1. Navigate to an agent's Memory tab.
2. Confirm storage-stats strip renders (even if count is 0).
3. Submit a memory-enabled task via the API or existing Submit page; wait for it to complete; refresh; confirm the entry appears in the list.
4. Click the entry → detail opens with title + summary + observations.
5. Click "Delete" → confirmation → entry disappears from the list.
6. Use the search bar with a query that matches an entry's summary → result appears.
7. Use the outcome filter to narrow to Succeeded / Failed.
8. Navigate to an agent with `memory.enabled=false` → tab still renders with the "Memory is disabled" notice.

## Acceptance Criteria

- [ ] The Memory tab is accessible at `/agents/:agentId/memory` from the AgentDetailPage tab list.
- [ ] The list page renders entries scoped to the current agent with outcome badges, task links, and delete buttons.
- [ ] Filters (outcome, date range) narrow the list correctly.
- [ ] The search input toggles between list endpoint (empty) and hybrid search endpoint (non-empty). Search results render a "Top N matches" label.
- [ ] The storage-stats strip renders `entry_count` + `approx_bytes` from the list endpoint's first page.
- [ ] At ≥ 80% of cap, the strip becomes a warning banner with a "Delete old entries" CTA.
- [ ] Clicking a row opens the entry detail; all fields from the detail endpoint are visible.
- [ ] "Attach to new task" navigates to `/submit?agentId=:id&attachMemoryId=:memory_id`.
- [ ] Delete flow: confirmation modal → DELETE → success toast → list refetches without the deleted entry.
- [ ] Memory-disabled agents show the informational notice; the tab remains functional for historical entries.
- [ ] Component tests cover the list, detail, delete, filter, and search flows using mocked API responses.
- [ ] Playwright browser verification passes: the scenarios listed above all work end-to-end with the real API and DB.

## Testing Requirements

- **Component tests** (Vitest + React Testing Library + MSW):
  - List renders with mocked response; filter change triggers new query.
  - Empty state when the list returns 0 items with no filters; "No results" state when filters are set.
  - Storage-stats strip renders; warning banner appears at 80%.
  - Entry detail renders all fields.
  - Delete dialog confirms + invokes mutation + shows toast.
  - Memory-disabled agent shows the notice.
- **Playwright (browser-verified):** Add a new scenario to `docs/CONSOLE_BROWSER_TESTING.md` covering the list → search → detail → delete flow on a memory-enabled agent with at least one completed task. This scenario is part of Task 11 but must be exercised during this task's development.

## Constraints and Guardrails

- Do not add Console UI for the Submit page in this task — Task 10 handles `attachMemoryId` in the Submit page.
- Do not implement a "regenerate summary" button; summary regeneration is explicitly deferred in the design doc.
- Do not render the summarizer model id as a selector — it is read-only display only.
- Do not render raw checkpoint / message history in the detail view — that path is existing task detail, not memory.
- Do not implement diff / version history for memory entries in v1. `updated_at` + `version` are shown, but there is no "view previous version" link.
- Do not add tag-based filtering UI in v1 (tags are displayed, not filtered).
- Do not cross-query memory across agents; the tab is strictly scoped by the route's `agentId`.
- Do not block Console render on the storage stats — treat them as optional; if the first page response omits `agent_storage_stats`, hide the strip rather than show a placeholder.

## Assumptions

- Task 3 has shipped; the Memory API endpoints respond with the documented shapes.
- Task 2 has shipped; the agent detail API surfaces `agent_config.memory.enabled` and `max_entries` so the Console can render the "memory disabled" notice and the warning threshold.
- Console auth / tenant scoping is handled by the existing layer (no new wiring in this task).
- shadcn/ui, React Query, and the existing routing library are used — no new dependencies.
- Playwright MCP tools + `make start` stack are functional for browser verification.

<!-- AGENT_TASK_END: task-9-console-memory-tab.md -->
