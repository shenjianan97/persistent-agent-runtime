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
- The dialog is scrollable and all fields are fillable: display name, model selector, system prompt, temperature, tools, human-in-the-loop toggle, max concurrent tasks, budget/task, budget/hour
- Budget fields show dollar conversion below the microdollar input
- After creating, the agent appears in the list
- Clicking the agent name navigates to `/agents/:agentId`
- The detail page shows read-only mode by default
- `Edit` switches to edit mode with form inputs plus `Save Changes` and `Cancel`
- `Cancel` reverts to read-only mode without saving
- Changing a field and clicking `Save Changes` persists the change and shows success toast `Agent updated`

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
| Dashboard feature | 1 |
| Cross-cutting layout, sidebar, routing, or API client changes | All |
| Backend-only change with no UI impact | None |

## Adding New Scenarios

- When implementing a new page or major UI feature, add a corresponding scenario before marking the task done
- When a browser verification session reveals a bug not covered by existing scenarios, add a regression scenario after fixing it
- Keep scenarios at the level of what to verify, not step-by-step tool instructions
- Number new scenarios sequentially as `Scenario 10`, `Scenario 11`, and so on, with descriptive titles
- Update the scenario-selection matrix when a new scenario maps to a change type
