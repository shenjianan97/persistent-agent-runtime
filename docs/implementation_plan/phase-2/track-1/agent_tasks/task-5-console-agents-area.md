<!-- AGENT_TASK_START: task-5-console-agents-area.md -->

# Task 5 — Console: Agents Area

## Agent Instructions

You are a software engineer implementing one module of a larger system. Your scope is strictly limited to this task.

**CRITICAL PRE-WORK:** Before beginning implementation, you MUST read:
1. `docs/design/phase-2/track-1-agent-control-plane.md` — canonical design contract (Console UX section)
2. `services/console/src/features/settings/SettingsPage.tsx` — existing CRUD page pattern (Langfuse endpoint management)
3. `services/console/src/layout/Sidebar.tsx` — navigation structure
4. `services/console/src/App.tsx` — routing
5. `services/console/src/api/client.ts` — API client patterns
6. `services/console/src/features/submit/SubmitTaskPage.tsx` — model fetching and form patterns

**CRITICAL POST-WORK:** After completing this task, update the status in `docs/implementation_plan/phase-2/track-1/progress.md` to "Done".

## Context

Track 1 adds a dedicated Agents area to the Console as a first-class navigation destination. This includes an Agents list page at `/agents` and an Agent detail page at `/agents/:agentId`. The Agents area is not a Settings subsection — it gets its own sidebar navigation item.

The Agents list page is the management entrypoint for creating and browsing agents. The Agent detail page shows the full current configuration and allows editing.

## Task-Specific Shared Contract

- Agents area is a first-class navigation destination, not a Settings subsection.
- Create happens from the list page via a dialog.
- Detail page shows full config, allows editing, and includes a "Submit Task" CTA.
- "Submit Task" CTA navigates to `/tasks/new?agent_id=<id>` and is hidden/disabled when agent is disabled.
- Follow existing dark-mode styling, shadcn/ui components, React Hook Form + Zod, TanStack Query v5 patterns.
- Agent list is lightweight — display name (primary), agent_id (secondary), provider, model, status badge.

## Affected Component

- **Service/Module:** Console (React 19 + TypeScript + Vite + Tailwind/shadcn)
- **File paths:**
  - `services/console/src/features/agents/AgentsListPage.tsx` (new)
  - `services/console/src/features/agents/AgentDetailPage.tsx` (new)
  - `services/console/src/features/agents/CreateAgentDialog.tsx` (new)
  - `services/console/src/features/agents/useAgents.ts` (new — TanStack Query hooks)
  - `services/console/src/types/index.ts` (modify — add Agent types)
  - `services/console/src/api/client.ts` (modify — add agent CRUD methods)
  - `services/console/src/App.tsx` (modify — add routes)
  - `services/console/src/layout/Sidebar.tsx` (modify — add Agents nav item)
- **Change type:** new code + modifications

## Dependencies

- **Must complete first:** Task 2 (Agent CRUD API endpoints must be available)
- **Provides output to:** Task 6 (agents area must exist for navigation links from task views)
- **Shared interfaces/contracts:** Agent API response types, agent CRUD API client methods

## Implementation Specification

### Step 1: Add Agent TypeScript types

Add to `services/console/src/types/index.ts`:

```typescript
export interface AgentSummaryResponse {
    agent_id: string;
    display_name: string;
    provider: string;
    model: string;
    status: 'active' | 'disabled';
    created_at: string;
    updated_at: string;
}

export interface AgentResponse {
    agent_id: string;
    display_name: string;
    agent_config: {
        system_prompt: string;
        provider: string;
        model: string;
        temperature: number;
        allowed_tools: string[];
    };
    status: 'active' | 'disabled';
    created_at: string;
    updated_at: string;
}

export interface AgentCreateRequest {
    agent_id: string;
    display_name: string;
    agent_config: {
        system_prompt: string;
        provider: string;
        model: string;
        temperature?: number;
        allowed_tools?: string[];
    };
}

export interface AgentUpdateRequest {
    display_name: string;
    agent_config: {
        system_prompt: string;
        provider: string;
        model: string;
        temperature?: number;
        allowed_tools?: string[];
    };
    status: 'active' | 'disabled';
}
```

### Step 2: Add agent CRUD methods to API client

Add to the `api` object in `services/console/src/api/client.ts`.

**IMPORTANT — Pattern:** The API client is a plain object literal (`export const api = { ... }`) using arrow functions that call the standalone `fetchApi()` function. It is NOT a class with `this._request()`. Follow the existing pattern:

```typescript
// Add these to the api object alongside existing methods like submitTask, listTasks, etc.
createAgent: (request: AgentCreateRequest) =>
    fetchApi<AgentResponse>('/v1/agents', {
        method: 'POST',
        body: JSON.stringify(request),
    }),

listAgents: (status?: string, limit?: number) => {
    const params = new URLSearchParams();
    if (status) params.set('status', status);
    if (limit) params.set('limit', limit.toString());
    const query = params.toString();
    return fetchApi<AgentSummaryResponse[]>(`/v1/agents${query ? '?' + query : ''}`);
},

getAgent: (agentId: string) =>
    fetchApi<AgentResponse>(`/v1/agents/${encodeURIComponent(agentId)}`),

updateAgent: (agentId: string, request: AgentUpdateRequest) =>
    fetchApi<AgentResponse>(`/v1/agents/${encodeURIComponent(agentId)}`, {
        method: 'PUT',
        body: JSON.stringify(request),
    }),
```

### Step 3: Create TanStack Query hooks

Create `services/console/src/features/agents/useAgents.ts`:

```typescript
// useAgents(status?) — list query, refetch on interval or on invalidation
// useAgent(agentId) — detail query for single agent
// useCreateAgent() — mutation with agents list invalidation
// useUpdateAgent() — mutation with agent detail + list invalidation
```

Follow patterns from existing hooks (e.g., `useTaskList`, `useLangfuseEndpoints`). Use query keys like `['agents', status]` and `['agent', agentId]`.

### Step 4: Create AgentsListPage

Create `services/console/src/features/agents/AgentsListPage.tsx`:

- Page title: "Agents"
- Status filter dropdown: All / Active / Disabled
- "Create Agent" button opening `CreateAgentDialog`
- Table/list with columns: Display Name (primary, clickable), Agent ID (secondary), Provider, Model, Status (badge), Created
- Clicking a row navigates to `/agents/:agentId`
- Follow existing table styling from `TaskListPage`
- Status badges: `active` → green/success, `disabled` → muted/gray

### Step 4b: Install Dialog shadcn/ui component

The `Dialog` component is not currently installed in the project. Install it before creating the dialog:

```bash
cd services/console && npx shadcn@latest add dialog
```

This creates `services/console/src/components/ui/dialog.tsx` with `Dialog`, `DialogContent`, `DialogHeader`, `DialogTitle`, `DialogFooter`, etc.

### Step 5: Create CreateAgentDialog

Create `services/console/src/features/agents/CreateAgentDialog.tsx`:

- Dialog/modal form with fields:
  - `agent_id` — text input, slug format validation
  - `display_name` — text input, max 200 chars
  - `system_prompt` — textarea
  - Provider/Model — grouped dropdown (reuse model fetching from `useModels()` hook)
  - Temperature — number input (0-2, step 0.1)
  - Allowed Tools — checkboxes (web_search, read_url, calculator)
- Validation: Zod schema matching API constraints
- On success: invalidate agents list, close dialog, show success toast
- On error: show error message in dialog

### Step 6: Create AgentDetailPage

Create `services/console/src/features/agents/AgentDetailPage.tsx`:

- Header: Display name (large) + Agent ID (secondary) + Status badge
- Editable form for:
  - Display Name
  - System Prompt (textarea)
  - Provider/Model selector
  - Temperature
  - Allowed Tools (checkboxes)
  - Status toggle (active/disabled)
- "Save" button calling `PUT /v1/agents/{agentId}`
- "Submit Task" CTA button:
  - Navigates to `/tasks/new?agent_id=<id>`
  - Hidden or visually disabled when agent status is `disabled`
- Loading state while fetching agent
- Error state if agent not found (404)
- Follow existing form styling patterns

### Step 7: Add routes

Add to `services/console/src/App.tsx`:

```tsx
<Route path="/agents" element={<AgentsListPage />} />
<Route path="/agents/:agentId" element={<AgentDetailPage />} />
```

### Step 8: Add sidebar navigation

Add to `services/console/src/layout/Sidebar.tsx` NAV_ITEMS array, positioned after Home and before Tasks:

```tsx
{ path: '/agents', label: 'Agents', icon: Bot, end: true },
```

Import `Bot` from `lucide-react`.

## Acceptance Criteria

- [ ] `/agents` route renders agents list with status filter and "Create Agent" button
- [ ] Clicking "Create Agent" opens a dialog with all required fields and validation
- [ ] Creating an agent shows success feedback and refreshes the list
- [ ] Clicking an agent row navigates to `/agents/:agentId`
- [ ] `/agents/:agentId` renders full agent detail with editable form
- [ ] Saving changes on detail page updates the agent via PUT and shows feedback
- [ ] Status toggle between `active` and `disabled` works correctly
- [ ] "Submit Task" CTA links to `/tasks/new?agent_id=<id>` and is disabled when agent is disabled
- [ ] Sidebar shows "Agents" nav item with Bot icon between Home and Tasks
- [ ] UI follows existing dark-mode brutalist styling conventions
- [ ] Empty state when no agents exist shows appropriate message

## Testing Requirements

- **Component tests:** Verify agents list renders with mock data. Create dialog opens and closes. Detail page loads and renders form fields.
- **Manual verification:** Full CRUD lifecycle via Console against running API: create agent, view in list, open detail, edit config, toggle status, use "Submit Task" CTA.

## Constraints and Guardrails

- Reuse shadcn/ui components (`Button`, `Card`, `Table`, `Badge`, `Input`, `Textarea`, `Select`, `Checkbox`, `Dialog`, `Form`). Do not introduce new UI libraries.
- Follow existing React Hook Form + Zod pattern for all forms.
- Follow existing TanStack Query patterns for data fetching and mutations.
- Reuse model fetching (`useModels()`) from the existing submit page for provider/model selection.
- Do not add task-history panels to the agent detail page — that is out of scope for Track 1.

## Assumptions

- The `Bot` icon exists in `lucide-react` (it does — it's a standard icon).
- The existing `useModels()` hook and model data structures can be reused for the provider/model selector.
- The `_request()` method in the API client handles error responses and throws `ApiError`.

<!-- AGENT_TASK_END: task-5-console-agents-area.md -->
