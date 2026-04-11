<!-- AGENT_TASK_START: task-7-console-updates.md -->

# Task 7 — Console: Agent Budget Form, Task Pause Rendering, Resume Action

## Agent Instructions

You are a software engineer implementing one module of a larger system. Your scope is strictly limited to this task. Do not modify components outside the "Affected Component" listed below.

**CRITICAL PRE-WORK:** Before beginning implementation, you MUST read:
1. `docs/design-docs/phase-2/track-3-scheduler-and-budgets.md` — canonical design contract (Console Design section, API Design section for response shapes)
2. `services/console/src/features/agents/AgentDetailPage.tsx` — existing agent detail/edit form
3. `services/console/src/features/agents/AgentsListPage.tsx` — existing agent list table
4. `services/console/src/features/agents/CreateAgentDialog.tsx` — existing agent creation modal
5. `services/console/src/features/task-detail/TaskDetailPage.tsx` — existing task detail page
6. `services/console/src/features/task-list/TaskListPage.tsx` — existing task list page
7. `services/console/src/features/task-detail/TaskStatusBadge.tsx` — existing status badge rendering

**CRITICAL POST-WORK:** After completing this task:
1. Run the full test suite with `make test` and verify all existing tests still pass. Fix any regressions before proceeding.
2. Update the status in `docs/exec-plans/active/phase-2/track-3/progress.md` to "Done".

## Context

Track 3 extends the Console with:
1. Agent detail/create forms: editable fields for `max_concurrent_tasks`, `budget_max_per_task`, `budget_max_per_hour`
2. Agent list: display budget/concurrency columns
3. Task views: distinguish budget-paused tasks from HITL-paused tasks, show recovery mode
4. Task detail: Resume button for per-task budget pauses
5. Task list: show `pause_reason` and `resume_eligible_at`
6. Task events timeline: budget pause/resume events with details

## Task-Specific Shared Contract

- Budget values are in microdollars. Display as dollars with appropriate formatting (e.g., "$0.50" for 500000 microdollars).
- The Resume button is only visible for tasks with `pause_reason === 'budget_per_task'`.
- Hourly budget pauses show "Auto-recovers" with the `resume_eligible_at` timestamp.
- Per-task budget pauses show "Requires budget increase" with a link/action to resume.
- The `TaskStatusBadge` should distinguish budget-paused from HITL-paused via the `pause_reason` field.
- The agent form validates that budget/concurrency values are positive integers.

## Affected Component

- **Service/Module:** Console (React SPA)
- **File paths:**
  - `services/console/src/features/agents/AgentDetailPage.tsx` (modify — add budget/concurrency form fields)
  - `services/console/src/features/agents/AgentsListPage.tsx` (modify — add budget columns)
  - `services/console/src/features/agents/CreateAgentDialog.tsx` (modify — add budget fields to create form)
  - `services/console/src/features/task-detail/TaskDetailPage.tsx` (modify — show pause details, add Resume button)
  - `services/console/src/features/task-list/TaskListPage.tsx` (modify — show pause_reason column)
  - `services/console/src/features/task-detail/TaskStatusBadge.tsx` (modify — enhance paused badge with sub-label)
  - Agent/Task hooks or API client files as needed (modify — add resume API call, update types)
- **Change type:** modification

## Dependencies

- **Must complete first:** Task 6 (API Extensions — budget fields on Agent responses, pause fields on Task responses, resume endpoint)
- **Provides output to:** Task 8 (Integration Tests — console tests)
- **Shared interfaces/contracts:** API response shapes from Task 6

## Implementation Specification

### Step 1: Update TypeScript types

Update the type definitions in `services/console/src/types/index.ts`. The actual type names in the codebase are `AgentResponse`, `AgentSummaryResponse`, `AgentCreateRequest`, `AgentUpdateRequest`, `TaskStatusResponse`, and `TaskSummaryResponse` — NOT generic `Agent`/`Task` interfaces.

**Agent types — update `AgentResponse`, `AgentSummaryResponse`, `AgentCreateRequest`, `AgentUpdateRequest`:**
```typescript
// Add to AgentResponse and AgentSummaryResponse:
max_concurrent_tasks: number;
budget_max_per_task: number;
budget_max_per_hour: number;

// Add to AgentCreateRequest and AgentUpdateRequest (optional):
max_concurrent_tasks?: number;
budget_max_per_task?: number;
budget_max_per_hour?: number;
```

**Task types — update `TaskStatusResponse` and `TaskSummaryResponse`:**
```typescript
// Add to TaskStatusResponse:
pause_reason?: 'budget_per_task' | 'budget_per_hour' | null;
pause_details?: {
  budget_max_per_task?: number;
  budget_max_per_hour?: number;
  observed_task_cost_microdollars?: number;
  observed_hour_cost_microdollars?: number;
  recovery_mode?: 'manual_resume_after_budget_increase' | 'automatic_after_window_clears';
} | null;
resume_eligible_at?: string | null;

// Add to TaskSummaryResponse:
pause_reason?: 'budget_per_task' | 'budget_per_hour' | null;
resume_eligible_at?: string | null;
```

### Step 2: Add agent budget form fields

In `AgentDetailPage.tsx`, add three new form fields to the agent edit form, grouped under a "Scheduling & Budget" section:

```tsx
{/* Scheduling & Budget */}
<FormField
  label="Max Concurrent Tasks"
  type="number"
  min={1}
  value={form.max_concurrent_tasks}
  onChange={...}
/>
<FormField
  label="Budget per Task"
  type="number"
  min={1}
  value={form.budget_max_per_task}
  onChange={...}
  helperText={formatUsd(form.budget_max_per_task)}
/>
<FormField
  label="Budget per Hour"
  type="number"
  min={1}
  value={form.budget_max_per_hour}
  onChange={...}
  helperText={formatUsd(form.budget_max_per_hour)}
/>
```

Display the dollar equivalent as helper text using the existing `formatUsd()` utility from `@/lib/utils` (line 8 of `utils.ts`) — do NOT use inline `.toFixed(2)` arithmetic.

Add the same fields to `CreateAgentDialog.tsx` with defaults (5, 500000, 5000000).

**Update the `onSubmit` handler** in `AgentDetailPage.tsx` (lines 70-99) to include the three new fields in the API request payload. These are top-level agent fields (NOT nested under `agent_config`).

**Update the `useEffect` form reset** (lines 56-68) to include the three new fields when resetting from loaded agent data.

Update the form schema validation:
```typescript
max_concurrent_tasks: z.number().int().min(1).default(5),
budget_max_per_task: z.number().int().min(1).default(500000),
budget_max_per_hour: z.number().int().min(1).default(5000000),
```

### Step 3: Add agent list budget columns

In `AgentsListPage.tsx`, add columns to the agent list table:

```tsx
<TableHeader>
  {/* ... existing columns */}
  <th>Max Tasks</th>
  <th>Budget/Task</th>
  <th>Budget/Hour</th>
</TableHeader>
```

Format budget values as dollars using the existing `formatUsd()` utility from `@/lib/utils` (used in `TaskListPage.tsx` at line 136 for cost display).

### Step 4: Enhance TaskStatusBadge for budget pauses

Update `TaskStatusBadge.tsx` to distinguish budget-paused tasks:

```tsx
function TaskStatusBadge({ status, pauseReason }: { status: string; pauseReason?: string | null }) {
  if (status === 'paused' && pauseReason) {
    const label = pauseReason === 'budget_per_task'
      ? 'Budget (Task)'
      : pauseReason === 'budget_per_hour'
        ? 'Budget (Hourly)'
        : 'Paused';
    return <Badge variant="warning">{label}</Badge>;
  }
  // ... existing status rendering
}
```

The existing "Paused" badge (gray) should now show more specific labels for budget pauses. Use a warning/amber color for budget pauses to distinguish them from generic pauses.

**Update all `TaskStatusBadge` call sites** to pass the new `pauseReason` prop:
1. `TaskDetailPage.tsx` line 130: `<TaskStatusBadge status={task.status} pauseReason={task.pause_reason} />`
2. `TaskListPage.tsx` line 133: `<TaskStatusBadge status={task.status as TaskStatus} pauseReason={task.pause_reason} className="..." />`

### Step 5: Add pause details to task detail page

In `TaskDetailPage.tsx`, add a pause info section visible only when the task is paused:

```tsx
{task.status === 'paused' && task.pause_reason && (
  <PauseInfoPanel>
    <dt>Pause Reason</dt>
    <dd>{formatPauseReason(task.pause_reason)}</dd>

    {task.pause_details && (
      <>
        <dt>Budget Limit</dt>
        <dd>{formatUsd(task.pause_details.budget_max_per_task || task.pause_details.budget_max_per_hour)}</dd>
        <dt>Observed Cost</dt>
        <dd>{formatUsd(task.pause_details.observed_task_cost_microdollars || task.pause_details.observed_hour_cost_microdollars)}</dd>
        <dt>Recovery</dt>
        <dd>{task.pause_details.recovery_mode === 'automatic_after_window_clears'
          ? `Auto-recovers${task.resume_eligible_at ? ` at ${formatDate(task.resume_eligible_at)}` : ''}`
          : 'Requires budget increase + manual resume'
        }</dd>
      </>
    )}

    {task.pause_reason === 'budget_per_task' && (
      <ResumeButton taskId={task.task_id} />
    )}
  </PauseInfoPanel>
)}
```

### Step 6: Add Resume button and API call

Add a Resume button component that calls `POST /v1/tasks/{task_id}/resume`:

```tsx
function ResumeButton({ taskId }: { taskId: string }) {
  const resumeMutation = useMutation({
    mutationFn: () => apiClient.post(`/v1/tasks/${taskId}/resume`),
    onSuccess: () => {
      queryClient.invalidateQueries(['task', taskId]);
      toast.success('Task resumed');
    },
    onError: (error) => {
      toast.error(error.response?.data?.message || 'Resume failed');
    },
  });

  return (
    <Button
      onClick={() => resumeMutation.mutate()}
      disabled={resumeMutation.isLoading}
      variant="primary"
    >
      Resume Task
    </Button>
  );
}
```

The Resume button should only be visible when `task.pause_reason === 'budget_per_task'`. For hourly pauses, show "Auto-recovers" text instead of a button.

### Step 7: Add pause_reason to task list

In `TaskListPage.tsx`, add `pause_reason` and `resume_eligible_at` information:

- Show `pause_reason` as a sub-label on the status column for paused tasks (via the updated `TaskStatusBadge` prop)
- For hourly pauses, show a relative time until `resume_eligible_at` (e.g., "in 23 min")
- Add a `pause_reason` filter dropdown to the task list filter bar (alongside the existing `status` and `agent_id` filters), enabling queries like `?status=paused&pause_reason=budget_per_task`

### Step 8: Update task events timeline for budget events

The existing `CheckpointTimeline` component renders event details by extracting keys from `detail?.message`, `detail?.prompt`, or `detail?.reason` (lines 319-324 of `CheckpointTimeline.tsx`). Budget events use different keys (`pause_reason`, `budget_max_per_task`, `observed_task_cost_microdollars`, `resume_trigger`). Extend the `detailText` extraction logic to handle these budget-specific keys:

```tsx
// Add after existing detail text extraction:
if (detail?.pause_reason) {
  const limit = detail.budget_max_per_task || detail.budget_max_per_hour;
  const observed = detail.observed_task_cost_microdollars || detail.observed_hour_cost_microdollars;
  detailText = `${formatUsd(observed)} / ${formatUsd(limit)} limit`;
} else if (detail?.resume_trigger) {
  detailText = detail.resume_trigger === 'automatic_hourly_recovery'
    ? 'Hourly budget cleared'
    : 'Operator action';
}
```

The component should render:

- `task_paused` with `details.pause_reason === 'budget_per_task'`: show "Paused: task budget exceeded ($X.XX / $Y.YY)"
- `task_paused` with `details.pause_reason === 'budget_per_hour'`: show "Paused: hourly budget exceeded ($X.XX / $Y.YY)"
- `task_resumed` with `details.resume_trigger === 'automatic_hourly_recovery'`: show "Resumed: hourly budget cleared"
- `task_resumed` with `details.resume_trigger === 'manual_operator_resume'`: show "Resumed: operator action"

## Acceptance Criteria

- [ ] Agent detail form shows editable fields for max_concurrent_tasks, budget_max_per_task, budget_max_per_hour
- [ ] Agent create dialog includes budget/concurrency fields with defaults
- [ ] Budget values display dollar equivalents (e.g., "$0.50" for 500000)
- [ ] Agent form validates that all three values are positive integers
- [ ] Agent list table shows max_concurrent_tasks, budget/task, budget/hour columns
- [ ] `TaskStatusBadge` shows "Budget (Task)" or "Budget (Hourly)" for budget-paused tasks
- [ ] Task detail shows pause info panel for paused tasks with reason, costs, and recovery mode
- [ ] Resume button appears only for `budget_per_task` paused tasks
- [ ] Resume button calls `POST /v1/tasks/{id}/resume` and shows success/error toast
- [ ] Hourly pauses show "Auto-recovers at [time]" instead of Resume button
- [ ] Task list shows pause_reason for paused tasks
- [ ] Task events timeline renders budget pause/resume events with formatted details

## Testing Requirements

- **Unit tests:** AgentDetailPage renders budget fields and validates input. TaskStatusBadge renders correct label for each pause_reason. Resume button calls correct API endpoint. PauseInfoPanel renders correct details for each pause type.
- **Integration tests:** Create agent with budget fields → verify form submission. Mock a budget-paused task → verify detail page renders pause info and Resume button.
- **Failure scenarios:** Resume API returns 409 → error toast displayed. Agent form submitted with invalid budget → validation error shown.

## Constraints and Guardrails

- Do not implement any backend logic — Task 6 provides the API.
- Follow the existing component patterns and styling conventions in the console codebase.
- Use the existing API client and react-query patterns for the resume call.
- Budget values are stored in microdollars — always convert for display.
- Do not add a separate scheduler page — extend existing Agent and Task views.

## Assumptions

- Task 6 has been completed (API exposes budget fields and resume endpoint).
- The existing API client and react-query hooks can be extended for the new fields.
- The existing form field components support `type="number"` with `min` validation.
- The existing toast notification system is available for success/error feedback.

<!-- AGENT_TASK_END: task-7-console-updates.md -->
