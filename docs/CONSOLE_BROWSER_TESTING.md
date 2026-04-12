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

### Scenario 10: Task File Attachments

What it validates: File attachment affordances on task submission work in a real browser.

What to verify:

- `/tasks/new` shows the file attachment drop zone in a disabled state until a sandbox-enabled agent is selected
- After selecting a sandbox-enabled agent, clicking `Drop files here or click to browse` opens the browser file picker exactly once
- Choosing a file adds it to the attachment list with filename and size shown
- Dragging and dropping a file onto the same surface also adds it to the attachment list
- Disabled state does not open the browser file picker
- No console errors appear during click-to-browse or drag-and-drop interactions

## When to Run Which Scenarios

| Change type | Required scenarios |
|-------------|-------------------|
| Any console change | 1 |
| Agent management feature | 1, 2, 3 |
| Task submission feature | 1, 3 |
| Task submission file attachment feature | 1, 3, 10 |
| Task detail / timeline feature | 1, 4 |
| Task list feature | 1, 5 |
| Budget / pause feature | 1, 4, 6 |
| Dead letter feature | 1, 7 |
| HITL / approval / input feature | 1, 4, 8 |
| Settings / Langfuse feature | 1, 9 |
| Dashboard feature | 1 |
| Cross-cutting layout, sidebar, routing, or API client changes | All |
| Backend-only change with no UI impact | None |

## Adding New Scenarios

- When implementing a new page or major UI feature, add a corresponding scenario before marking the task done
- When a browser verification session reveals a bug not covered by existing scenarios, add a regression scenario after fixing it
- Keep scenarios at the level of what to verify, not step-by-step tool instructions
- Number new scenarios sequentially as `Scenario 10`, `Scenario 11`, and so on, with descriptive titles
- Update the scenario-selection matrix when a new scenario maps to a change type
