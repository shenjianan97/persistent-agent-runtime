# Console Browser Testing

This document defines browser-based verification scenarios for AI agents using Playwright MCP browser tools.

## Prerequisites

- `make start` must be running
- Console available at `http://localhost:5173`
- API available at `http://localhost:8080`

These are not coded tests. They are verification instructions for AI agents to follow with browser tools after implementing console changes.

## Tool Usage Tips

- `browser_snapshot`: preferred for structural assertions such as headings, text content, dialogs, table columns, and form state
- `browser_take_screenshot`: use for visual and layout checks such as alignment, spacing, wrapping, and general page presentation
- `browser_click`, `browser_fill_form`, `browser_select_option`: use for user interactions
- `browser_console_messages`: check for uncaught errors, React warnings, and failed requests; this is the primary way to detect silent failures
- `browser_evaluate`: run JavaScript assertions or extract values from the page
- `browser_wait_for`: handle async loading, React Query polling, and status transitions
- If the first snapshot shows `Loading...`, wait briefly and snapshot again

## Scenario Authoring Rules

Every Console task runs Scenario 1 + every scenario the §When to Run Which Scenarios table maps to the change. This section is the **canonical source** for authoring rules; AGENTS.md and CONSOLE_TASK_CHECKLIST.md defer here.

**Definitions.** *Parity guard* = a single sub-object must render identically (same fields, labels, defaults, and visibility rules) on every surface that shows it; Template D enforces this. *Coverage matrix* = sub-object × surface grid below. *Selection matrix* = change-type → scenarios table near the bottom of this file.

Rules (release-blocking):

1. New page / dialog / form / tab → new scenario (Template B).
2. New field on an existing form → extend the scenario that covers that form. Extending means **asserting the field by name and `data-testid`**, not merely mentioning the sub-section exists.
3. Every interactive element has a stable `data-testid`.
4. Same commit updates the §Agent-Config Coverage Matrix (if the change touches an agent-config sub-object) *and* the §When to Run Which Scenarios selection matrix.
5. Scenarios state *what* to verify, not tool-by-tool steps.
6. If the feature adds rendering of a sub-object to >1 surface, Template D is required **regardless of how many cells the author chose to cite**. The trigger is the code, not the matrix.

## Agent-Config Coverage Matrix

**Scope:** this matrix applies to **agent-config sub-objects only** (fields on `POST /v1/agents` / `PUT /v1/agents/:id`). Non-config UI additions (new tabs, new pages, filter controls, etc.) add scenarios via the selection matrix but do not appear here.

**Cell legend:** `N` = Scenario N asserts this cell (field-level, not just presence). `N + M` = both scenarios required; each covers part of the cell. `—` = surface does not render this sub-object by design. `⚠ gap` = surface renders it but no scenario asserts it; the gap MUST have a matching row in [tech-debt-tracker.md](./exec-plans/tech-debt-tracker.md) AND be closed before the next feature that touches the sub-object merges.

Adding a new sub-object or surface adds a row/column and populates every cell (scenario number or `—`) in the same commit. Filling a `⚠ gap` removes its tech-debt entry.

| Sub-object | Create Dialog | Agent Detail | Edit Form | Submit (read-only) | Task Detail |
|---|---|---|---|---|---|
| `core` (name / prompt / model / temperature) | 2 | 2 | 2 | 3 | — |
| `tools` | 2 | 2 | 2 | 3 | — |
| `memory` | 2 | 11 | 2 + 11 | 12, 13, 14 | 12, 13 |
| `context_management` | 2 + 16 | 2 | 2 + 15 | — | 17 |
| `sandbox` | 2 | 2 | 2 | 10 (gating only) | — |
| `budget` | 2 | 2 | 2 | 3 | 6 |
| `HITL` (task-level, not agent-config) | — | — | — | — | 8 |
| `max_concurrent_tasks` | 2 | 2 | 2 | 3 | — |

## Scenario Templates

Copy the matching template's assertions into a new scenario (or into the extended bullets of an existing scenario — the assertions must appear somewhere in a scenario that's cited from the coverage matrix, not just inline in code).

- **A — Config field on one surface:** field renders with label + `data-testid`; default matches API; edit round-trips through save + reload; update the coverage-matrix cell.
- **B — New dialog / tab:** entry affordance has `data-testid`; opens on click; empty + populated states both render; close/navigate-away is clean; `browser_console_messages` empty.
- **C — List + filter + detail:** list columns render; each filter narrows then clears correctly; row click routes to detail; back-nav preserves filters.
- **D — Cross-cutting config (>1 surface):** required whenever a sub-object's **code** renders on >1 surface (see Rule 6). Assert: sub-object renders on every non-`—` surface in its coverage-matrix row; labels + defaults identical across surfaces; write-surface round-trip visible on every read-surface; visibility/enable rules applied identically everywhere.

**Worked example — adding a new agent-config sub-object across Create + Edit + Submit surfaces:**
1. Extend Scenario 2 to assert the new sub-section in the Create dialog AND the Edit form (field names, defaults, `data-testid`s).
2. Extend Scenario 3 to assert the read-only view on the Submit page.
3. Add a new row to the Coverage Matrix: `Create Dialog = 2`, `Agent Detail = 2` (if rendered), `Edit Form = 2`, `Submit = 3` (or `—`), `Task Detail = —` (typically).
4. Apply Template D's four assertions in the extended Scenario 2 (identical labels / defaults / round-trip / visibility rules across surfaces).
5. Tick the checklist in your task spec; orchestrator runs Playwright post-merge.

## Standard Scenarios

Each scenario describes what to verify, not exact click sequences. Agents should translate these checks into the right Playwright MCP calls.

### Scenario 1: Navigation Smoke Test

What it validates: All pages render without crashing.

What to verify:

- Navigate to each route: `/`, `/tasks`, `/tasks/new`, `/agents`, `/dead-letter`, `/settings`
- Each page shows its heading without blank screen. Sidebar nav labels are: Home, Agents, Tasks, Submit Task, Failed, Settings
- No console errors; use `browser_console_messages` to check for uncaught exceptions or React error boundaries
- Dashboard (`/`) renders 5 summary cards with numeric values, not `NaN` or blank

### Scenario 2: Agent CRUD Lifecycle

What it validates: Full agent management flow.

What to verify:

- `/agents` renders agent list with columns: Name, Agent ID, Provider, Model, Status, Max Tasks, Budget/Task, Budget/Hour, Created
- `Create Agent` opens a dialog, not a new page
- The dialog is scrollable and all core fields are fillable: display name, model selector, system prompt, temperature, tools, max concurrent tasks, budget/task, budget/hour
- Budget fields show dollar conversion below the microdollar input
- **Sandbox sub-section** renders with an enable toggle; enabling reveals template, vCPU, memory (MB), and timeout (seconds) inputs. Each input has a stable `data-testid`.
- **Memory sub-section** renders with an enable toggle; enabling reveals summarizer-model selector and max-entries input.
- **Context-management sub-section** renders (always visible — no enable toggle) with summarizer-model dropdown (`data-testid="context-management-summarizer-model"`), exclude-tools chip input (`data-testid="context-management-exclude-tools"`), and pre-Tier-3 memory-flush toggle (`data-testid="context-management-pre-tier3-flush"`, disabled when memory is off).
- After creating, the agent appears in the list; values set in the sandbox / memory / context-management sub-sections are present on the detail page
- Clicking the agent name navigates to `/agents/:agentId`
- The detail page shows read-only mode by default
- `Edit` switches to edit mode with form inputs plus `Save Changes` and `Cancel`. The edit form renders the same sandbox / memory / context-management sub-sections as the create dialog, with identical labels, defaults, and visibility rules (parity guard — see Template D)
- `Cancel` reverts to read-only mode without saving
- Changing a field in any sub-section and clicking `Save Changes` persists the change and shows success toast `Agent updated`; reloading the page surfaces the saved values

### Scenario 3: Task Submission Flow

What it validates: End-to-end task dispatch.

What to verify:

- `/tasks/new` shows an agent selector populated with active agents
- Selecting an agent reveals `Agent Configuration (read-only)` with model, temperature, system prompt, tools, max concurrent tasks, budget/task, and budget/hour
- Budget values are dollar formatted
- Execution parameters show max steps, max retries, and task timeout
- Task input textarea is fillable
- Submitting redirects to `/tasks/:taskId`
- The new task appears in `/tasks`

### Scenario 4: Task Detail & Execution Timeline

What it validates: Task monitoring view completeness.

What to verify:

- Cost summary shows 4 cards: Total Cost, Checkpoints, Tokens, Duration
- Duration label says `Execution time`, not `Total runtime`, and excludes pause time
- Execution Timeline renders checkpoint steps with step number, event type badge, and timestamp
- Each step footer shows 4 columns: Worker, Step Cost, Total Cost, Duration
- Step costs use `+$X.XX` format and total cost accumulates correctly
- HITL events such as `task_paused` and `task_resumed` render inline with detail text
- Input and Output panels render at the bottom
- Non-terminal tasks show a `Cancel` button

### Scenario 5: Task List Filtering

What it validates: Filter controls work correctly.

What to verify:

- `/tasks` shows filter controls for Status, Agent ID, and Pause Reason
- Status options are: All, Queued, Running, Awaiting Approval, Awaiting Input, Paused, Completed, Cancelled, Failed
- Selecting a status filters the list correctly
- Agent ID filtering works with debounce
- Pause Reason options are: All, Task Budget Exceeded, Hourly Budget Exceeded
- Clearing filters restores the full list

### Scenario 6: Budget Pause & Resume

What it validates: Budget enforcement is visible and actionable in the UI.

What to verify:

- A paused task shows `Task Budget Exceeded` or `Hourly Budget Exceeded`, not `Budget (Task)`
- The task detail timeline shows pause details with cost versus limit, for example `$0.018 / $0.005 limit`
- Hourly pauses show `Recovers in X min` in the task list
- Per-task budget pauses show `Resume Task` on the detail page
- After increasing the budget on the agent and clicking `Resume Task`, the task resumes
- The timeline shows `Task Resumed` with detail like `Budget raised to $X.XX (task cost: $Y.YY)`
- The Duration card excludes paused time

### Scenario 7: Dead Letter & Redrive

What it validates: Failed task management.

What to verify:

- `/dead-letter` shows failed tasks with error reason and agent info
- Agent ID filtering narrows the list
- Empty state shows `No failed tasks` when applicable
- `Redrive` re-queues the task and shows success toast `Task redrive initiated`
- After redrive, the page navigates to the redriven task detail page
- Redrive is also available from the task detail page for dead-lettered tasks

### Scenario 8: HITL Approval & Input Flows

What it validates: Human-in-the-loop interactive panels.

What to verify:

- Tasks in `waiting_for_approval` show an Approval Panel with Approve and Reject actions
- Approve transitions the task back to running
- Reject supports a reason and marks the task accordingly
- Tasks in `waiting_for_input` show an Input Response Panel with text input and submit action
- Submitting input transitions the task back to running
- These panels are visible only in the corresponding task state

### Scenario 9: Settings - Langfuse Endpoints

What it validates: Observability endpoint management.

What to verify:

- `/settings` renders the Langfuse endpoint list or empty state
- Endpoints display name, host, and status
- The create endpoint dialog opens and is fillable
- After creating an endpoint, it appears in the list
- Deleting an endpoint removes it from the list

### Scenario 11: Agent Memory Tab

Covers design-doc acceptance criteria AC-9 (Console browse / search / read / delete / storage stats), AC-10 (memory-disabled agent still renders historical entries), and AC-13 (80%-of-cap warning banner).

What it validates: Memory tab on the Agent detail page renders, filters, searches, opens detail, and deletes entries. Covers the memory-disabled and 80%-of-cap variants.

Preconditions:

- At least one agent with `agent_config.memory.enabled = true` exists. Use the API (`POST /v1/agents`) or the Settings / Agent dialog once memory toggling surfaces.
- (For the "list with entries" checks) at least one completed memory-enabled task has executed so the list is non-empty. If the worker write path (Task 6) has not shipped yet, use a temporary `INSERT INTO agent_memory_entries (...)` via `psql` against the dev DB to seed at least one row per scope.

What to verify:

- Navigating to `/agents/:agentId` shows two tabs — `Overview` (active) and `Memory` — when the agent has memory enabled. Only `Overview` shows when memory is disabled.
- Clicking the `Memory` tab navigates to `/agents/:agentId/memory` and renders the Memory tab content.
- The storage-stats strip at the top shows an entry count (`N of 10,000 entries`) and approximate bytes (e.g., `~12.3 MB`). With `entry_count = 0`, the strip still renders; no warning banner appears.
- With `entry_count >= 0.8 * max_entries`, the strip becomes an amber warning banner with a `Delete old entries` button. With `entry_count >= max_entries`, the banner is red and references FIFO trim.
- The filter bar exposes: outcome dropdown (All / Succeeded / Failed), `From` date input, `To` date input, search input, `Search` button, and a `Clear` button (visible when any filter is set).
- Selecting `outcome = Failed` updates the list to show only failed entries (or the "No results match your filters" empty state).
- Typing a query and pressing `Search` switches the view to search mode with a `Top 20 matches` label plus a `ranking: hybrid | text | vector` badge. Clearing the search restores the list view.
- Clicking a row navigates to `/agents/:agentId/memory/:memoryId` and renders: title, outcome badge, created/updated timestamps, summary, observations list (in order), linked task link (deep-linked to `/tasks/:taskId`), summarizer model id, tags, an `Attach to new task` button, and a `Delete` button.
- Clicking `Attach to new task` navigates to `/tasks/new?agentId=<id>&attachMemoryId=<memory_id>` (Task 10 picks up the query param).
- Clicking `Delete` on the detail view (or the per-row delete button in the list) opens a confirmation dialog with the entry title and a "cannot be undone" notice. Confirming deletes the entry, shows a `Memory entry deleted` toast, and removes the row from the list.
- Navigating to a memory-disabled agent's Memory tab still renders the tab (when visited directly via URL) with a dismissible notice: `Memory is disabled for this agent. Existing entries are preserved; no new entries will be written.` Historical entries remain browsable and deletable.
- `browser_console_messages` shows no uncaught exceptions during any of the above flows.

### Scenario 10: Task File Attachments

What it validates: File attachment affordances on task submission work in a real browser.

What to verify:

- `/tasks/new` shows the file attachment drop zone in a disabled state until a sandbox-enabled agent is selected
- After selecting a sandbox-enabled agent, clicking `Drop files here or click to browse` opens the browser file picker exactly once
- Choosing a file adds it to the attachment list with filename and size shown
- Dragging and dropping a file onto the same surface also adds it to the attachment list
- Disabled state does not open the browser file picker
- No console errors appear during click-to-browse or drag-and-drop interactions

### Scenario 12: Submit-Page Memory Attach

Covers design-doc acceptance criteria AC-8 (customer attach at submission with validated scope + persisted `attached_memory_ids`) and AC-11 (`skip_memory_write` per-task override propagates on the wire).

What it validates: The Submit-page memory-attach widget, token-footprint indicator, `skip_memory_write` toggle, and deep-link pre-selection are wired end-to-end.

What to verify:

- `/tasks/new` does NOT render the `Memory` card when the selected agent has `agent_config.memory.enabled = false` or the field is absent
- Selecting an agent with `agent_config.memory.enabled = true` reveals a `Memory` card containing the `Attach Past Memories` picker (initially closed) and the `Skip writing a memory entry for this task` checkbox
- Clicking `Browse` expands the picker and triggers a `GET /v1/agents/:agent_id/memory` call (confirm via `browser_network_requests`). The initial expand is lazy — no memory API hits before that
- Typing in the search input switches the endpoint from `/memory` to `/memory/search?q=...`; clearing the input falls back to `/memory`
- Clicking an entry in the picker adds it to the `Selected` panel below, in position order; clicking the `X` removes it
- The `Attached context:` indicator renders with a byte approximation and entry count. Selecting a large entry (≥10 KB combined summary + observations) turns the indicator amber and surfaces a tooltip on hover
- The `(N/50)` counter on the picker reflects the current selection size; selecting a 51st entry no-ops and surfaces a capped-selection warning
- Submitting the form with attached memories POSTs a JSON body that contains `attached_memory_ids: [...]` in selection order and `memory_mode` set to the dropdown value (`always` | `agent_decides` | `skip`). Verify via `browser_network_requests` on the `/v1/tasks` POST
- After a successful submit, navigate to `/tasks/:taskId` and confirm the `TaskStatusResponse` exposes `attached_memory_ids` and `attached_memories_preview` with the attached entries — inspect with `browser_evaluate` against the task detail endpoint
- Deep link: navigating to `/tasks/new?agent_id=<memory-enabled-agent>&attachMemoryId=<valid-memory-id>` auto-selects that entry in the Selected panel
- Deep link mismatch: navigating with `attachMemoryId` pointing at a memory-DISABLED agent surfaces a toast along the lines of `Attachment ignored — memory is disabled for this agent` and does NOT render the Memory card
- Clicking `Attach to new task` from the Memory tab (Task 9) opens `/tasks/new?agent_id=...&attachMemoryId=...` with the entry pre-selected
- Switching the selected agent after a selection clears the Selected list (no cross-agent leak)
- No console errors during any of the flows above

### Scenario 13: Memory End-to-End Cross-Feature Flow

Covers Task 11's "Memory Tab E2E" and "Submit Attach E2E" requirements in a single cross-feature session that exercises every Console-visible stop on the memory journey. Primarily validates design-doc acceptance criteria AC-2 (one entry per completed memory-enabled task), AC-8 (attachment persisted on the submitted task), AC-9 (customer-visible browse / search / read / delete), and AC-11 (`memory_mode=skip` honoured end-to-end).

Preconditions:

- `make start` stack running with the API at `:8080` and the Console at `:5173`.
- Two agents created via `POST /v1/agents` — one with `agent_config.memory.enabled = true` (the "memory-enabled agent") and one with memory disabled (or absent).
- At least one past completed task on the memory-enabled agent so the Memory tab is non-empty. Seed via running a real task (preferred) or, as a fallback, a temporary `INSERT INTO agent_memory_entries ...`.

What to verify, in order (a single browser session):

1. **Memory Tab E2E**: navigate to `/agents/:memory_enabled_agent_id/memory`. Confirm the list renders with at least one entry, the storage-stats strip shows entry count + bytes, and filters / search behave per Scenario 11. Open a row (detail view), assert title, summary, observations, linked task, tags, summarizer model id render. Delete a row via the detail page's `Delete` button, confirm the toast and the entry disappearing from the list.
2. **Submit Attach E2E**: from the detail page of a different (still-present) memory entry, click `Attach to new task`. Confirm routing to `/tasks/new?agent_id=<memory_enabled_agent_id>&attachMemoryId=<memory_id>` with that entry pre-selected. Open the picker, add a second entry, leave the memory-mode dropdown at its default ("Always save memory"), submit. Confirm the POST body carries `attached_memory_ids` in the correct order and `memory_mode: "always"` (via `browser_network_requests`).
3. **Task detail surfaces attachments**: navigate to `/tasks/:taskId`, confirm the Attachments / Memory section lists the two attached memory entries (preview titles). Use `browser_evaluate` against `/v1/tasks/:taskId` to assert the response has `attached_memory_ids: [...]` and `attached_memories_preview: [...]` with the same two ids.
4. **Wait for completion**: poll until the task reaches `completed` (may take several seconds against a live worker). Once completed, re-open the Memory tab for the same agent — confirm a NEW entry appears for this task, with title / summary populated, and that it survives a page reload.
5. **`memory_mode="skip"` variant**: submit a second task with the memory-mode dropdown set to "Don't save memory". After completion, re-open the Memory tab and assert **no** new entry was written for the task (AC-11).
6. **Memory-disabled agent negative path**: navigate to the disabled agent's `/tasks/new`. The memory-mode dropdown (`memory-mode-select`) renders but is locked to "Don't save memory" and disabled with helper text "This agent has memory disabled"; the attach picker (`attach-memory-picker`) does NOT render. Submitting a task there produces no memory row. Navigate to that agent's Memory tab and confirm the disabled notice + any historical entries render as read-only.
7. `browser_console_messages` shows zero uncaught exceptions across the full walkthrough.

### Scenario 14: Task Memory Mode Dropdown

Covers Track 5 Task 12 — the `memory_mode` submission field replaces the old `skip_memory_write` checkbox with a three-value dropdown whose options are `always`, `agent_decides`, and `skip`. Verifies the `agent_decides` end-to-end flow (with and without the agent calling `save_memory`), the disabled-when-memory-off branch, and the cross-field API validation.

Preconditions: two agents seeded — `agent-memory-on` (`agent_config.memory.enabled = true`) and `agent-memory-off` (`agent_config.memory.enabled = false`).

1. Navigate to Submit Task. Select `agent-memory-on`. Assert the `memory-mode-select` trigger exists, defaults to "Always save memory", and is enabled.
2. Change the dropdown to "Let agent decide". Submit a task whose prompt instructs the agent to call `save_memory(reason="test reason")`. After completion, navigate to the Memories page and assert a new row appears for this task. Open task detail; confirm the `memory_mode` metadata reads `agent_decides` and the timeline includes the `save_memory` tool call carrying the reason.
3. Repeat with a prompt that does NOT call `save_memory`. Assert no memory row is created, no "Memory Saved" timeline marker appears, and task detail shows `memory_mode = agent_decides`.
4. Change the dropdown to "Don't save memory". Submit a task. Assert no memory row is written, no memory-related timeline entries appear, and task detail shows `memory_mode = skip`.
5. Select `agent-memory-off`. Assert the dropdown snaps to "Don't save memory", is disabled, and the helper text reads "This agent has memory disabled". Submission succeeds and persists `memory_mode = skip`.
6. Craft a `POST /v1/tasks` (via devtools / `browser_evaluate`) for `agent-memory-off` with `memory_mode: "always"`. Assert the API responds 400 with a validation error referencing the memory-enabled invariant.

### Scenario 15: Context Management Section

Covers Track 7 Task 11 — the new "Context management" section on the Agent edit form. Verifies all three tuning fields persist end-to-end, the 50-entry cap is enforced, and the `pre_tier3_memory_flush` toggle correctly reflects memory-enabled state.

Preconditions: at least one agent exists. Both a memory-enabled agent (`agent_config.memory.enabled = true`) and a memory-disabled agent (or absent) are helpful for the disabled-toggle branch.

1. Navigate to `/agents/:agentId` and click `Edit`. Assert the `Context management` section renders after the `Memory` section, contains a `Summarizer Model` dropdown, an `Exclude Tools` chip input, and a `Pre-Tier-3 Memory Flush` checkbox. Assert there is NO `enabled` toggle for context management. The section header reads "Context management is always-on platform infrastructure; the fields below are tuning knobs, not an enable toggle."

2. Select a model from the `Summarizer Model` dropdown (`data-testid="context-management-summarizer-model"`). Type a tool name (e.g., `web_search`) into the chip input (`data-testid="context-management-exclude-tools"`) and press Enter. Confirm the chip appears. Toggle `pre_tier3_memory_flush` on (`data-testid="context-management-pre-tier3-flush"`). Click `Save Changes`. Navigate away and return to the agent. Re-enter edit mode and assert all three fields retain their saved values.

3. With 50 chips already entered in `exclude_tools`, type a 51st tool name and press Enter. Assert the inline error "Maximum 50 entries" appears. Assert the chip count stays at 50. Assert `Save Changes` is not blocked by this client-side error — the user can still save (the 51st entry was rejected, not added).

4. With a memory-disabled agent (or disable memory for the test agent), open edit mode. Assert the `Pre-Tier-3 Memory Flush` checkbox is visually disabled and a note reads "Requires memory to be enabled." Enable memory in the Memory section. Assert the `Pre-Tier-3 Memory Flush` checkbox becomes enabled.

5. Open edit mode without touching any context management field. Click `Save Changes`. Inspect the PUT request body via `browser_network_requests`. Assert the `agent_config` does NOT contain a `context_management` key (don't-send-defaults).

6. `browser_console_messages` shows no uncaught exceptions during any of the above flows.

### Scenario 16: Create Agent Context Management Parity

What it validates: The Create Agent dialog exposes the same context-management tuning controls as the edit form, and the create payload persists them correctly.

What to verify:

1. Navigate to `/agents` and click `Create Agent`. Assert the dialog opens as a modal and is scrollable.
2. In the create dialog, confirm a `Context Management` section renders after the `Memory` section. Assert it contains:
   - the `Summarizer Model` dropdown (`data-testid="context-management-summarizer-model"`)
   - the `Exclude Tools from Compaction` chip input (`data-testid="context-management-exclude-tools"`)
   - the `Pre-Tier-3 Memory Flush` checkbox (`data-testid="context-management-pre-tier3-flush"`)
   - no `Enable Context Management` toggle
3. With memory disabled, assert `Pre-Tier-3 Memory Flush` is disabled and the helper text reads `Requires memory to be enabled.`
4. Enable memory in the same dialog. Assert `Pre-Tier-3 Memory Flush` becomes enabled.
5. Select a context summarizer model, add at least one excluded tool chip, enable `Pre-Tier-3 Memory Flush`, then submit the form. Inspect the POST request body via `browser_network_requests` and assert `agent_config.context_management` contains the selected `summarizer_model`, `exclude_tools`, and `pre_tier3_memory_flush: true`.
6. **Partial-input faithfulness (P1 regression guard):** open a second create dialog. Set ONLY the `Summarizer Model`; leave the `Pre-Tier-3 Memory Flush` checkbox unchecked and do not add any excluded tools. Submit. Inspect the POST body and assert `agent_config.context_management.pre_tier3_memory_flush === false` — the key MUST be present and explicitly `false` (not missing, not `true`). Rationale: the worker defaults a missing value to `true`, so an absent key would silently override the UI's unchecked state.
7. **Summarizer × provider parity (P2 regression guard):** in a third create dialog, keep the default `provider = anthropic` and open the `Summarizer Model` dropdown. Assert every option belongs to the `anthropic` provider (no OpenAI / other-provider options visible). Then change the primary model to an OpenAI entry. Assert the summarizer dropdown re-renders with only OpenAI options; if a summarizer was previously selected on `anthropic`, it is cleared (the dropdown reverts to `Platform default`).
8. Re-open the created agent in `/agents/:agentId`, enter edit mode, and confirm the same context-management values are present there. Also confirm the edit form applies the same summarizer × provider filtering (only options for the agent's provider are visible).
9. `browser_console_messages` shows no uncaught exceptions during the flow.

### Scenario 17: Langfuse Trace — Context Window Management (Track 7 AC 14 manual)

Covers Track 7 Task 12 AC 14 (manual Langfuse UI portion). Verifies that a task
which exercises all three compaction tiers leaves the expected trace structure in
Langfuse: one `compaction.tier3` span per Tier 3 firing, one `compaction.inline`
span per call that fires Tier 1/1.5, and per-result cap annotations on affected
tool spans.

Preconditions: Langfuse is enabled (`LANGFUSE_ENABLED=true`) and the live stack
is running. Create an agent whose history will grow large enough to trigger all
three compaction tiers — use a small `context_management.summarizer_model` or a
model with a small context window so Tier 3 fires quickly.

1. Submit a task to the agent and let it run to completion (or at least until the
   compaction tiers fire). If Tier 3 did not fire, resubmit with more tool-heavy
   steps so the history token count exceeds the Tier 3 threshold.

2. Open Langfuse UI (default `http://localhost:3300`) and navigate to the trace
   for the task. Locate the root trace span.

3. Assert `compaction.inline` span(s) exist: at least one child span whose name
   is `compaction.inline` should appear for each LLM call that ran Tier 1 or Tier
   1.5. The span metadata/tags should include `tier: "1"` or `tier: "1.5"`.

4. Assert `compaction.tier3` span exists: exactly one child span per Tier 3
   firing should appear with name `compaction.tier3`. The span metadata should
   include `tokens_in`, `tokens_out`, `cost_microdollars`, and
   `summarizer_model_id`.

5. Assert per-result cap annotations: for each tool call whose result was capped,
   the tool span should carry an annotation or tag `result_capped: true` (or
   equivalent — check the structured-log event shape from AC 14 automated tests).

6. If a pre-Tier-3 memory flush fired, assert a `memory_flush` child span or
   annotation is visible on the trace.

7. `browser_console_messages` shows no uncaught exceptions in the Langfuse UI.

### Scenario 18: User-Facing Conversation Log

Covers Phase 2 Track 7 Task 13 — the user-facing "Conversation" tab on task detail. The tab renders the rendered entry stream from `GET /v1/tasks/:taskId/conversation` (what the user sees), separately from the existing Timeline tab (infrastructure events, operator audience). Verifies that long-context runs surface a `compaction_boundary` divider with an operator-visible provenance fold, and that the Timeline tab continues to render unchanged.

Preconditions:

- `make start` stack running with the API at `:8080` and the Console at `:5173`.
- One agent configured with `agent_config.context_management.tier3_trigger_fraction = 0.1` (low trigger fraction forces Tier 3 summarisation to fire quickly so the scenario can observe a `compaction_boundary`). If the console doesn't expose `tier3_trigger_fraction` directly, seed the config via the API.
- Fixture file on disk at `/tmp/large_log.txt` — at least ~200 KB of plain text. Create with any deterministic generator (e.g. `yes 'x' | head -c 200000 > /tmp/large_log.txt`) so the task's tool calls produce oversized outputs and trigger compaction.
- One in-flight task with at least one `hitl_pause` / `hitl_resume` cycle if available; otherwise skip those assertions but still run the rest.

Steps:

1. Submit a task against the configured agent whose prompt instructs it to `read_file` the `/tmp/large_log.txt` fixture several times (enough to exceed the 10% Tier-3 trigger). Navigate to `/tasks/:taskId`.
2. **Default tab is Conversation.** Without any query string, confirm `[data-testid="tab-conversation"]` carries `aria-selected="true"` and `[data-testid="conversation-pane"]` is present. The subtitle reads "What the agent did". `browser_snapshot` shows entry rows with kind-specific testids (`conversation-entry-user_turn`, `conversation-entry-agent_turn`, `conversation-entry-tool_call`, `conversation-entry-tool_result` at minimum).
3. **Compaction divider appears once Tier 3 fires.** Wait for the task to accumulate enough context to trigger compaction (poll task detail until checkpoints > ~10, or watch Langfuse for `compaction.tier3_fired`). Assert `[data-testid="conversation-compaction-divider"]` is present and its text matches `/Context summarized \(turns \d+–\d+, \d+ turns\)/`.
4. **Operator fold reveals provenance.** Click the compaction divider to expand. Assert the summary text is visible (primary reveal). Then click `[data-testid="conversation-operator-fold"]` to expand the secondary fold and confirm the four operator-only fields render: `summarizer_model`, `summary_bytes`, `cost_microdollars`, `tier3_firing_index`.
5. **Capped tool_result explicit copy.** Expand any `[data-testid="conversation-entry-tool_result"]` whose entry carries `metadata.capped=true` (reading `/tmp/large_log.txt` should trigger the per-result 25KB cap). Confirm the explicit copy renders: "Tool returned {orig_bytes} bytes; showing head+tail capped at 25KB (same view the model had)."
6. **Ingestion-offload inline notice (`offload_emitted`).** Once the task has produced at least one oversized tool result (the `read_url` or `read_file` calls against `/tmp/large_log.txt` cross `OFFLOAD_THRESHOLD_BYTES=20000`), assert `[data-testid="conversation-entry-offload_emitted"]` is present in the conversation stream. The inline notice text matches `/\d+ older tool outputs? archived \([0-9.]+ (B|KB|MB)\)/` and the entry is visually lighter than the `conversation-compaction-divider` (compact one-line banner, no full dashed border spanning the column). The notice MUST sit inline between the `tool_result` that triggered it and the next `agent_turn`, not at the top of the pane.
7. **Timeline-tab regression check.** Navigate to `/tasks/:taskId?tab=timeline`. Confirm `[data-testid="tab-timeline"]` is now `aria-selected="true"`, the checkpoint timeline renders exactly as before (`Execution Timeline` heading, step cards, HITL markers, cost/duration footer), and the Conversation pane is unmounted. Switch back to the Conversation tab via the tab button; confirm the URL drops the `?tab=timeline` parameter. `browser_console_messages` shows zero uncaught exceptions across the tab switches.

### Scenario 19: Unified Activity View (Task 8 — Conversation+Timeline unification)

Covers Phase 2 Track 7 Follow-up Task 8 — the unified "Activity" pane backed by `GET /v1/tasks/:taskId/activity`. Collapses the legacy "Conversation" + "Execution Timeline" split onto a single projection over `checkpoints` + `task_events`. The legacy tabs (and their backing code) were removed in Phase D; there is no feature flag — Activity is the only pane on task detail.

Preconditions:

- `make start` stack running; API at `:8080`, Console at `:5173`.
- One in-flight or completed task with at least one tool-call turn AND at least one marker in `task_events` (`task_compaction_fired`, `memory_flush`, `offload_emitted`, `task_paused`, or a lifecycle event). If no task meets both, seed one by running the Scenario 18 fixture task against an agent with low `tier3_trigger_fraction`.

Steps:

1. **Pane renders.** Navigate to `/tasks/:taskId`. Confirm `[data-testid="activity-pane"]` is present with header copy "Activity / What the agent did" and that `[data-testid="activity-summary"]` text matches `/^\d+ turns?$/` (turn count only — the markers count was removed because it leaks jargon). At least one `[data-testid^="activity-row-"]` element is present and each row carries a `data-kind` attribute — assert the set includes at minimum `turn.user` and either `turn.assistant` or `turn.tool` for a task that ran tool calls.
2. **Role-anchored rendering.** For a `turn.user` row, the row content contains the user's prompt text verbatim. For a `turn.tool` row, the row text contains the tool name (e.g. `read_url`, `read_file`). Assistant turns that issued tool calls render one `Tool call → <name>` fold per entry in `tool_calls` as full-width siblings of the assistant bubble; clicking the fold reveals the args JSON. `Tool result ← <name>` rows render as full-width green folds (destructive-tinted when `is_error`), right edges aligned with the tool-call folds above.
3. **Per-turn cost / usage.** Assistant pills carry `<Xk in → Y out · $Z>` badges (data-testids `activity-row-<i>-usage` + `activity-row-<i>-cost`). `$Z` is absent when the backend attributes zero cost to that turn (e.g. synthetic turns injected before any LLM call). The backend attributes checkpoint `cost_microdollars` to the AI message id that first appears in the checkpoint — verify by cross-referencing the `TOTAL COST` stat card against the sum of per-turn cost badges.
4. **Details toggle filters infra markers.** By default, infrastructure markers (`marker.memory_flush`, `marker.offload_emitted`, `marker.lifecycle`) are hidden. Click `[data-testid="activity-details-toggle"]`. The request fires with `include_details=true` (verifiable via `browser_network_requests`). Previously-hidden marker kinds now appear as rows *interleaved with turns by real timestamp* — lifecycle events MUST NOT stack at the top of the stream. `marker.compaction_fired` and `marker.hitl.*` remain visible regardless of the toggle.
5. **Per-row expander reveals raw payload.** Locate a `marker.compaction_fired` row. Click `[data-testid="activity-row-<i>-expand"]` on that row. `[data-testid="activity-row-<i>-details"]` appears inline with a JSON blob containing `tokens_in`, `tokens_out`, and `turns_summarized`. Click the expander again; the details block disappears.

## When to Run Which Scenarios

| Change type | Required scenarios |
|-------------|-------------------|
| Any console change | 1 |
| Agent management feature | 1, 2, 3 |
| Task submission feature | 1, 3 |
| Task submission file attachment feature | 1, 3, 10 |
| Task submission memory attach feature | 1, 3, 12 |
| Task detail / timeline feature | 1, 4 |
| Task list feature | 1, 5 |
| Budget / pause feature | 1, 4, 6 |
| Dead letter feature | 1, 7 |
| HITL / approval / input feature | 1, 4, 8 |
| Settings / Langfuse feature | 1, 9 |
| Agent Memory tab feature | 1, 11 |
| Task submission memory-mode / `agent_decides` feature | 1, 3, 14 |
| Cross-cutting memory feature / Track 5 verification | 1, 11, 12, 13, 14 |
| Agent context management section feature | 1, 2, 15, 16 |
| Context window management / compaction observability | 1, 17 |
| Task detail conversation log feature | 1, 18 |
| Task detail unified Activity view | 1, 4, 18, 19 |
| Dashboard feature | 1 |
| Cross-cutting layout, sidebar, routing, or API client changes | All |
| Backend-only change with no UI impact | None |

## Adding New Scenarios

See **§Scenario Authoring Rules** at the top of this file for the authoritative rules. Quick checklist:

- Number sequentially (next unused index — currently 18 and up).
- Pick a template from §Scenario Templates and copy its always-required assertions.
- Update the §When to Run Which Scenarios matrix and the §Agent-Config Coverage Matrix in the same commit.
- If fixing a bug not covered by an existing scenario, add a regression scenario before merging the fix.
