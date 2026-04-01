<!-- AGENT_TASK_START: task-6-console-updates.md -->

# Task 6 — Console Updates: Status Display, HITL UI, Events Timeline

## Agent Instructions

You are a software engineer implementing one module of a larger system. Your scope is strictly limited to this task. Do not modify components outside the "Affected Component" listed below.

**CRITICAL PRE-WORK:** Before beginning implementation, you MUST read:
1. `docs/design/phase-2/PHASE2_MULTI_AGENT.md` — Section 7 (Human-in-the-Loop Input)
2. `services/console/src/types/index.ts` — existing type definitions (TaskStatus, CheckpointEvent, etc.)
3. `services/console/src/features/task-detail/TaskDetailPage.tsx` — existing task detail layout and conditional rendering
4. `services/console/src/features/task-detail/CheckpointTimeline.tsx` — existing timeline rendering pattern
5. `services/console/src/features/dead-letter/DeadLetterPage.tsx` — redrive button pattern (for approve/reject/respond UX)
6. `services/console/src/api/client.ts` — existing API call patterns

**CRITICAL POST-WORK:** After completing this task, update the status in `docs/implementation_plan/phase-2/track-2/progress.md` to "Done".

## Context

Track 2 introduces three new task statuses and human-in-the-loop workflows. The Console must surface these clearly: new status badges, interactive approval/input panels on the task detail page, and a lifecycle events timeline that complements the existing checkpoint timeline.

This task covers all Console changes for Track 2 in a single spec because the UI changes are tightly coupled and should be implemented together for a coherent user experience.

## Task-Specific Shared Contract

- The three new statuses are `waiting_for_approval`, `waiting_for_input`, `paused`.
- API endpoints for HITL actions: `POST /v1/tasks/{id}/approve`, `POST /v1/tasks/{id}/reject`, `POST /v1/tasks/{id}/respond`.
- API endpoint for events: `GET /v1/tasks/{id}/events`.
- The task detail response now includes `pending_input_prompt`, `pending_approval_action`, `human_input_timeout_at`.
- Follow existing Console patterns: React Query for data fetching, shadcn/ui for components, Tailwind for styling, toast notifications for action results.

## Affected Component

- **Service/Module:** Console Frontend
- **File paths:**
  - `services/console/src/types/index.ts` (modify)
  - `services/console/src/api/client.ts` (modify)
  - `services/console/src/features/task-detail/TaskDetailPage.tsx` (modify)
  - `services/console/src/features/task-detail/ApprovalPanel.tsx` (new)
  - `services/console/src/features/task-detail/InputResponsePanel.tsx` (new)
  - `services/console/src/features/task-detail/TaskEventsTimeline.tsx` (new)
  - `services/console/src/features/task-list/TaskListPage.tsx` (modify — status filter)
  - `services/console/src/features/dashboard/DashboardPage.tsx` (modify — pending count)
  - Status badge component (modify — wherever status badges are rendered)
- **Change type:** modification + new code

## Dependencies

- **Must complete first:** Task 1 (Database Migration — new statuses exist), Task 2 (Event Service — GET events endpoint), Task 3 (HITL API — approve/reject/respond endpoints)
- **Provides output to:** Task 7 (Integration Tests — may include basic UI smoke tests)
- **Shared interfaces/contracts:** API response types from Tasks 2 and 3

## Implementation Specification

### Step 1: Update types

In `types/index.ts`, expand `TaskStatus`:
```typescript
export type TaskStatus = 'queued' | 'running' | 'completed' | 'cancelled' | 'dead_letter'
    | 'waiting_for_approval' | 'waiting_for_input' | 'paused';
```

Add event types:
```typescript
export type TaskEventType =
    'task_submitted' | 'task_claimed' | 'task_retry_scheduled' |
    'task_reclaimed_after_lease_expiry' | 'task_dead_lettered' |
    'task_redriven' | 'task_completed' | 'task_paused' | 'task_resumed' |
    'task_approval_requested' | 'task_approved' | 'task_rejected' |
    'task_input_requested' | 'task_input_received' | 'task_cancelled';

export interface TaskEventResponse {
    event_id: string;
    task_id: string;
    agent_id: string;
    event_type: TaskEventType;
    status_before?: string;
    status_after?: string;
    worker_id?: string;
    error_code?: string;
    error_message?: string;
    details?: Record<string, unknown>;
    created_at: string;
}

export interface TaskEventListResponse {
    events: TaskEventResponse[];
}
```

Update the task status response interface to include new fields:
```typescript
// Add to existing TaskStatusResponse or equivalent
pending_input_prompt?: string;
pending_approval_action?: {
    tool_name: string;
    tool_args: unknown;
    context?: string;
};
human_input_timeout_at?: string;
```

### Step 2: Update API client

In `api/client.ts`, add:

```typescript
export const approveTask = async (taskId: string): Promise<void> => {
    await fetchApi<void>(`/v1/tasks/${taskId}/approve`, { method: 'POST' });
};

export const rejectTask = async (taskId: string, reason: string): Promise<void> => {
    await fetchApi<void>(`/v1/tasks/${taskId}/reject`, {
        method: 'POST',
        body: JSON.stringify({ reason }),
    });
};

export const respondToTask = async (taskId: string, message: string): Promise<void> => {
    await fetchApi<void>(`/v1/tasks/${taskId}/respond`, {
        method: 'POST',
        body: JSON.stringify({ message }),
    });
};

export const getTaskEvents = async (taskId: string, limit = 100): Promise<TaskEventListResponse> => {
    return fetchApi<TaskEventListResponse>(`/v1/tasks/${taskId}/events?limit=${limit}`);
};
```

Use the existing shared `fetchApi()` helper (or add these methods onto the existing exported `api` object) rather than bare `fetch(...)` so HTTP errors surface consistently through the Console's existing `ApiError` handling and toast flows.

### Step 3: Update status badges

Wherever task status badges are rendered (find the existing badge component), add styles for the three new statuses:

- `waiting_for_approval` → amber/orange background, label "Awaiting Approval"
- `waiting_for_input` → blue/info background, label "Awaiting Input"
- `paused` → muted gray background, label "Paused"

Use the existing badge pattern (likely a colored span/div with rounded corners and text).

### Step 4: Create ApprovalPanel component

Create `ApprovalPanel.tsx`:

```tsx
interface ApprovalPanelProps {
    taskId: string;
    pendingAction: { tool_name: string; tool_args: unknown; context?: string };
    timeoutAt?: string;
    onActionComplete: () => void;
}
```

Layout:
- Header: "Approval Required"
- Display the pending tool call: tool name, arguments (formatted JSON)
- Context if available
- Timeout countdown showing time remaining until `human_input_timeout_at`
- Two action buttons:
  - "Approve" (primary) — calls `approveTask(taskId)`, shows success toast, calls `onActionComplete`
  - "Reject" — opens a text area for rejection reason, then calls `rejectTask(taskId, reason)`, shows success toast, calls `onActionComplete`
- Loading states during API calls
- Error handling with toast notifications

### Step 5: Create InputResponsePanel component

Create `InputResponsePanel.tsx`:

```tsx
interface InputResponsePanelProps {
    taskId: string;
    prompt: string;
    timeoutAt?: string;
    onActionComplete: () => void;
}
```

Layout:
- Header: "Input Requested"
- Display the agent's prompt/question
- Timeout countdown
- Text area for the human's response
- "Send Response" button — calls `respondToTask(taskId, message)`, shows success toast, calls `onActionComplete`
- Validation: require non-empty message
- Loading states and error handling

### Step 6: Create TaskEventsTimeline component

Create `TaskEventsTimeline.tsx`:

```tsx
interface TaskEventsTimelineProps {
    events: TaskEventResponse[];
}
```

Layout:
- Vertical timeline with events in chronological order (oldest at top)
- Each event shows:
  - Icon based on event type category
  - Event type label (human-readable: "Task Submitted", "Task Claimed", etc.)
  - Timestamp (relative, e.g., "2 minutes ago")
  - Status transition arrow: status_before → status_after (if both present)
  - Worker ID (if present, small text)
  - Error details (if present, in a muted expandable section)
- Color coding by event category:
  - Green: `task_completed`, `task_approved`, `task_input_received`
  - Amber: `task_retry_scheduled`, `task_reclaimed_after_lease_expiry`, `task_approval_requested`, `task_input_requested`
  - Red: `task_dead_lettered`, `task_rejected`, `task_cancelled`
  - Blue: `task_submitted`, `task_claimed`, `task_redriven`
- Empty state: "No lifecycle events recorded"

### Step 7: Integrate into TaskDetailPage

In `TaskDetailPage.tsx`:

1. **Add React Query hook for events:**
   ```typescript
   const { data: eventsData } = useQuery({
       queryKey: ['task-events', taskId],
       queryFn: () => getTaskEvents(taskId),
       refetchInterval: isTerminal ? false : 5000,  // Poll for non-terminal tasks
   });
   ```

2. **Conditionally render HITL panels:**
   - If status is `waiting_for_approval` and `pending_approval_action` exists → render `<ApprovalPanel />`
   - If status is `waiting_for_input` and `pending_input_prompt` exists → render `<InputResponsePanel />`
   - The `onActionComplete` callback should invalidate the task status query to trigger a re-fetch

3. **Add events timeline section:**
   - Below the existing CheckpointTimeline
   - Section header: "Lifecycle Events"
   - Render `<TaskEventsTimeline events={eventsData?.events ?? []} />`

4. **Update cancel button visibility:**
   - Cancel should be available for `waiting_for_approval`, `waiting_for_input`, `paused` states (in addition to existing `queued`, `running`)

### Step 8: Update TaskListPage status filter

In `TaskListPage.tsx`, update the status filter dropdown to include the new statuses:
- Add "Awaiting Approval", "Awaiting Input", "Paused" options

### Step 9: Update Dashboard with pending action count

In `DashboardPage.tsx`, add a metric card or badge showing the count of tasks in waiting states. This can use the existing `listTasks` API with status filters:
- Fetch tasks with `status=waiting_for_approval` + `status=waiting_for_input`
- Display count as "Pending Actions: N" or similar

## Acceptance Criteria

- [ ] Status badges render correctly for `waiting_for_approval`, `waiting_for_input`, `paused`
- [ ] Task list status filter includes new statuses
- [ ] ApprovalPanel renders for `waiting_for_approval` tasks with pending action details
- [ ] ApprovalPanel Approve button calls API and shows success toast
- [ ] ApprovalPanel Reject button opens reason input, calls API, shows toast
- [ ] InputResponsePanel renders for `waiting_for_input` tasks with agent prompt
- [ ] InputResponsePanel Send button validates non-empty, calls API, shows toast
- [ ] Both panels show timeout countdown
- [ ] TaskEventsTimeline renders events in chronological order with correct colors/icons
- [ ] Events timeline polls for non-terminal tasks
- [ ] Cancel button available for waiting states
- [ ] Dashboard shows pending action count
- [ ] Console production build (`npm run build`) succeeds without errors

## Testing Requirements

- **Manual testing:** Navigate to task detail for each new status. Verify panels render. Submit approve/reject/respond. Verify status updates. Verify events timeline populates.
- **Build verification:** `npm run build` in `services/console/` produces no errors.
- **TypeScript:** No type errors after adding new types.

## Constraints and Guardrails

- Follow existing Console patterns: shadcn/ui components, Tailwind classes, React Query hooks, toast notifications.
- Do not introduce new npm dependencies unless absolutely necessary.
- Do not restructure existing components — add new ones and modify existing ones minimally.
- Use the existing dark-mode styling pattern from the recent Console refresh.
- Do not add WebSocket support — polling via React Query refetchInterval is the existing pattern.

## Assumptions

- Tasks 1, 2, and 3 are complete (API endpoints exist and return correct data).
- The existing Console build and dev setup work correctly.
- The existing toast notification pattern is available for reuse.
- The `pending_approval_action` JSONB structure is `{ tool_name, tool_args, context? }`.

<!-- AGENT_TASK_END: task-6-console-updates.md -->
