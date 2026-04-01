<!-- AGENT_TASK_START: task-6-console-submit-task-views.md -->

# Task 6 — Console: Submit Page Rework + Task Views

## Agent Instructions

You are a software engineer implementing one module of a larger system. Your scope is strictly limited to this task.

**CRITICAL PRE-WORK:** Before beginning implementation, you MUST read:
1. `docs/design/phase-2/track-1-agent-control-plane.md` — canonical design contract (Submit page and Task presentation sections)
2. `services/console/src/features/submit/SubmitTaskPage.tsx` — current inline config form (to be reworked)
3. `services/console/src/features/submit/schema.ts` — current Zod schema
4. `services/console/src/features/task-list/TaskListPage.tsx` — task list to enrich
5. `services/console/src/features/task-detail/TaskDetailPage.tsx` — task detail to enrich
6. `services/console/src/features/dead-letter/DeadLetterPage.tsx` — dead letter page to enrich

**CRITICAL POST-WORK:** After completing this task, update the status in `docs/implementation_plan/phase-2/track-1/progress.md` to "Done".

## Context

The submit page changes from "define an agent inline" to "run a task with an existing agent." The user selects an active agent from a dropdown, sees a read-only preview of the agent's config, then provides task-level inputs. Task views (list, detail, dead letter) are updated to show `agent_display_name` alongside `agent_id`.

## Task-Specific Shared Contract

- Submit page must not offer inline agent creation or inline agent configuration.
- If no agents exist, show an inline empty state with a link to `/agents` — do not redirect.
- If `?agent_id=` query param references a missing or disabled agent, show an error and require choosing another.
- Task views show `display_name` as primary label, `agent_id` as secondary, with navigation link to `/agents/:agentId`.
- The `agent_display_name` field in task responses may be null for pre-Track-1 tasks.

## Affected Component

- **Service/Module:** Console (React 19 + TypeScript + Vite + Tailwind/shadcn)
- **File paths:**
  - `services/console/src/features/submit/SubmitTaskPage.tsx` (rewrite)
  - `services/console/src/features/submit/schema.ts` (modify — remove config fields)
  - `services/console/src/features/submit/useSubmitTask.ts` (modify — update payload)
  - `services/console/src/features/task-list/TaskListPage.tsx` (modify — show display_name)
  - `services/console/src/features/task-detail/TaskDetailPage.tsx` (modify — show display_name)
  - `services/console/src/features/dead-letter/DeadLetterPage.tsx` (modify — show display_name)
  - `services/console/src/api/client.ts` (modify — update `submitTask()` payload shape)
  - `services/console/src/types/index.ts` (modify — update request/response types)
- **Change type:** modification (submit page is a substantial rewrite)

## Dependencies

- **Must complete first:** Task 3 (new submission contract), Task 4 (display_name in responses), Task 5 (agents area and API client methods exist)
- **Provides output to:** None (final Console task)
- **Shared interfaces/contracts:** Agent types and API methods from Task 5. Task response types with `agent_display_name` from Task 4.

## Implementation Specification

### Step 1: Update TypeScript types

In `services/console/src/types/index.ts`:

- `TaskSubmissionRequest`: remove `agent_config` nesting. Keep: `agent_id`, `input`, `max_steps`, `max_retries`, `task_timeout_seconds`, `langfuse_endpoint_id`.
- `TaskSubmissionResponse`: add `agent_display_name: string | null` field.
- `TaskStatusResponse`: add `agent_display_name: string | null` field.
- `TaskSummaryResponse`: add `agent_display_name: string | null` field.
- `DeadLetterItemResponse`: add `agent_display_name: string | null` field.

### Step 2: Update API client submitTask()

In `services/console/src/api/client.ts`, update `submitTask()`:

```typescript
async submitTask(request: TaskSubmissionRequest): Promise<TaskSubmissionResponse> {
    return this._request('POST', '/v1/tasks', {
        agent_id: request.agent_id,
        input: request.input,
        max_steps: request.max_steps,
        max_retries: request.max_retries,
        task_timeout_seconds: request.task_timeout_seconds,
        langfuse_endpoint_id: request.langfuse_endpoint_id,
    });
}
```

Remove any `agent_config` nesting from the payload construction.

### Step 3: Update submit page schema

In `services/console/src/features/submit/schema.ts`:

Remove all config-related fields from the Zod schema: `system_prompt`, `provider`, `model`, `temperature`, `allowed_tools`.

Keep: `agent_id` (required string), `input` (required string), `max_steps`, `max_retries`, `task_timeout_seconds`, `langfuse_endpoint_id`.

### Step 4: Rework SubmitTaskPage

Replace the inline config form with an agent-selector-based layout:

**Agent Selection Section:**
- Dropdown/combobox listing active agents (fetch via `listAgents(status='active')` from Task 5's hooks)
- Show both `display_name` and `agent_id` in the dropdown options
- Support `?agent_id` query parameter: read from `useSearchParams()`, preselect matching agent
- If `?agent_id` references a missing or disabled agent, show error alert and require manual selection

**Agent Config Preview Section** (shown after selection):
- Read-only summary of the selected agent's config: system prompt, provider/model, temperature, tools
- Fetch agent detail via `getAgent(agentId)` when agent is selected
- Visually distinct from editable fields (e.g., muted background, no edit controls)

**Task Inputs Section** (preserved from current form):
- Task input (textarea)
- Max steps, max retries, task timeout (number inputs with existing validation)
- Langfuse endpoint selector (optional dropdown)

**Empty State:**
- If no agents exist (`listAgents` returns empty), show inline message:
  - "No agents exist yet. Create an agent before submitting a task."
  - Link to `/agents` page
- Do not redirect automatically
- Do not offer inline agent creation

**Submit Button:**
- Enabled only when an agent is selected and required fields are filled
- Calls `submitTask()` with the new payload shape

### Step 5: Update task list page

In `services/console/src/features/task-list/TaskListPage.tsx`:

- Replace the current `agent_id` column with a combined display:
  - Display Name as primary text (if available)
  - Agent ID as secondary/muted text below or beside it
- Make the agent identity clickable, linking to `/agents/:agentId`
- Handle null `agent_display_name` gracefully (show only `agent_id` for pre-Track-1 tasks)

### Step 6: Update task detail page

In `services/console/src/features/task-detail/TaskDetailPage.tsx`:

- Show `display_name` as primary agent label with `agent_id` as secondary
- Link to `/agents/:agentId` for full agent inspection
- Handle null `agent_display_name` gracefully

### Step 7: Update dead letter page

In `services/console/src/features/dead-letter/DeadLetterPage.tsx`:

- Same display convention as task list: display_name (primary) + agent_id (secondary)
- Handle null `agent_display_name` gracefully

## Acceptance Criteria

- [ ] Submit page shows agent selector dropdown instead of inline config form
- [ ] Selecting an agent fetches and shows read-only config preview
- [ ] `?agent_id=` query param preselects the matching active agent
- [ ] `?agent_id=` with missing/disabled agent shows error state
- [ ] Empty state shown when no agents exist (message + link to `/agents`, no redirect)
- [ ] Task list shows `display_name` (primary) + `agent_id` (secondary) with link to agent detail
- [ ] Task detail shows `display_name` (primary) + `agent_id` (secondary) with link to agent detail
- [ ] Dead letter list shows `display_name` + `agent_id`
- [ ] Null `agent_display_name` for pre-Track-1 tasks handled gracefully (no errors, shows agent_id only)
- [ ] Submit succeeds and creates task correctly with the new payload shape

## Testing Requirements

- **Component tests:** Submit page renders with and without agents. Agent preselection from query param works. Empty state shown when no agents. Config preview renders after selection.
- **Manual verification:** Full submit flow: select agent → preview config → fill task input → submit → verify task created. Navigate to task list, verify display names. Open task detail, click agent link.

## Constraints and Guardrails

- Do not offer inline agent creation on the submit page.
- Do not redirect from submit page if no agents exist — show inline empty state.
- Do not add agent config editing on the submit page — config is read-only preview.
- The submit page is not a secondary agent management surface.
- Task views link to agent detail pages but do not embed agent config details.

## Assumptions

- Task 5 has delivered the `listAgents()`, `getAgent()` API client methods and the `useAgents()` hooks.
- Task 3 has delivered the new submission API contract (no `agent_config` in request body).
- Task 4 has delivered `agent_display_name` in all task response types.
- The `useModels()` hook is no longer needed on the submit page (model comes from agent config).

<!-- AGENT_TASK_END: task-6-console-submit-task-views.md -->
