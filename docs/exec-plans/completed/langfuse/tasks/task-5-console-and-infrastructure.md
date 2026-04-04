<!-- AGENT_TASK_START: task-5-console-and-infrastructure.md -->

# Task 5: Console UI + Infrastructure Relocation

## Agent Instructions
You are a software engineer implementing one module of a larger system.
Your scope is strictly limited to this task. Do not modify components outside
the "Affected Component" listed below.

**CRITICAL PRE-WORK:** Before beginning implementation, you MUST read the following context files:
1. `docs/design-docs/langfuse/design.md`
2. `services/console/src/App.tsx` (current routes)
3. `services/console/src/api/client.ts` (API client)
4. `services/console/src/features/task-detail/ObservabilityTrace.tsx` (to be reworked)
5. `services/console/src/features/task-detail/CostSummary.tsx` (to be reworked)
6. `services/console/src/features/submit/SubmitTaskPage.tsx` (to add dropdown)
7. `infrastructure/local/langfuse/docker-compose.yml` (to be moved)
8. `Makefile` (to update targets)

## Context
The Console currently renders Langfuse trace data (LLM call details, tool I/O) fetched via the API service's observability endpoint. With the refactoring, the Console shows only platform-owned data from checkpoints. Additionally, a new Settings page allows customers to manage their registered Langfuse endpoints, and the task submission form gains an optional endpoint selector. The Langfuse docker-compose moves from platform infrastructure to a test fixture.

## Task-Specific Shared Contract
- Console NEVER displays data from Langfuse — only platform-owned checkpoint data.
- Customers manage Langfuse endpoints via a Settings page (CRUD).
- Task submission includes an optional Langfuse endpoint dropdown.
- The observability response shape has changed (Task 4) — no spans, no trace_id, checkpoint-based items only.
- Langfuse docker-compose is a test fixture, not platform infrastructure.

## Affected Component
- **Service/Module:** Console Frontend, Infrastructure, Makefile
- **File paths:** `services/console/src/`, `infrastructure/local/langfuse/`, `tests/fixtures/langfuse/`, `Makefile`, `.env.localdev.example`
- **Change type:** new code + modification + relocation

## Dependencies
- **Must complete first:** Task 2 (API CRUD endpoints), Task 3 (worker cost data), Task 4 (simplified observability response)
- **Provides output to:** None (final integration task)
- **Shared interfaces/contracts:** `/v1/langfuse-endpoints` API, simplified `/v1/tasks/{id}/observability` response.

## Implementation Specification

### Step 1: TypeScript Types

Add to `services/console/src/types/` (or wherever types are defined):

```typescript
interface LangfuseEndpoint {
    endpoint_id: string;
    tenant_id: string;
    name: string;
    host: string;
    created_at: string;
    updated_at: string;
}

interface LangfuseEndpointRequest {
    name: string;
    host: string;
    public_key: string;
    secret_key: string;
}

interface LangfuseEndpointTestResponse {
    reachable: boolean;
    message: string;
}
```

Add `langfuse_endpoint_id?: string` to the task submission request and task status response types.

### Step 2: API Client Methods

Modify `services/console/src/api/client.ts`:
- `createLangfuseEndpoint(request: LangfuseEndpointRequest): Promise<LangfuseEndpoint>`
- `listLangfuseEndpoints(): Promise<LangfuseEndpoint[]>`
- `getLangfuseEndpoint(endpointId: string): Promise<LangfuseEndpoint>`
- `updateLangfuseEndpoint(endpointId: string, request: LangfuseEndpointRequest): Promise<LangfuseEndpoint>`
- `deleteLangfuseEndpoint(endpointId: string): Promise<void>`
- `testLangfuseEndpoint(endpointId: string): Promise<LangfuseEndpointTestResponse>`

Modify the `submitTask` method to include `langfuse_endpoint_id` when present.

### Step 3: Settings Page — Langfuse Endpoint Management

Create new files in `services/console/src/features/settings/`:

`SettingsPage.tsx`:
- Container page with heading "Settings"
- Section "Langfuse Endpoints" containing the endpoint list
- Follow existing page patterns (layout, styling)

`LangfuseEndpointList.tsx`:
- Table displaying registered endpoints: name, host, created date
- Row actions: Edit, Delete, Test Connection
- "Add Endpoint" button
- Empty state: "No Langfuse endpoints configured"

`LangfuseEndpointDialog.tsx`:
- Modal/dialog for create and edit
- Fields: Name, Host URL, Public Key, Secret Key
- Secret fields use password input type
- "Test Connection" button that calls the test endpoint and shows result
- Submit button: "Create" or "Update" depending on mode

`useLangfuseEndpoints.ts`:
- React Query hooks:
  - `useLangfuseEndpoints()` — list query
  - `useCreateLangfuseEndpoint()` — mutation
  - `useUpdateLangfuseEndpoint()` — mutation
  - `useDeleteLangfuseEndpoint()` — mutation
  - `useTestLangfuseEndpoint()` — mutation

### Step 4: Route and Navigation

Modify `services/console/src/App.tsx`:
- Import `SettingsPage`
- Add route: `<Route path="/settings" element={<SettingsPage />} />`

Modify sidebar/navigation (likely `AppShell.tsx` or a sidebar component):
- Add "Settings" nav item with a Settings/gear icon (from lucide-react)
- Place it at the bottom of the navigation, before any existing footer items

### Step 5: Task Submission Form

Modify `services/console/src/features/submit/SubmitTaskPage.tsx`:
- Add an optional "Langfuse Endpoint" dropdown/select field
- Populate options from `useLangfuseEndpoints()` query
- First option: "None" (default — no Langfuse)
- If no endpoints are registered, show text: "No endpoints configured — set up in Settings"
- Pass `langfuse_endpoint_id` to the submit mutation when selected

If a form schema file exists (e.g., `schema.ts`), add `langfuse_endpoint_id: z.string().uuid().optional()` or equivalent.

### Step 6: Rework ObservabilityTrace

Modify `services/console/src/features/task-detail/ObservabilityTrace.tsx`:
- Remove all Langfuse span rendering (LLM call details, tool call I/O, span types `llm_span`, `tool_span`, `system_span`)
- Keep checkpoint/runtime event rendering: `checkpoint_persisted`, `resumed_after_retry`, `completed`, `dead_lettered`
- Each checkpoint item shows: step number, cost, input/output tokens, timestamp
- Remove `trace_id` display
- Remove `spans` processing from the response

### Step 7: Rework CostSummary

Modify `services/console/src/features/task-detail/CostSummary.tsx`:
- Data now comes from checkpoint aggregates in the observability response (same endpoint, different response shape)
- Total cost, token counts, checkpoint count still available
- Per-step cost chart: source from checkpoint items' `cost_microdollars` instead of Langfuse span costs
- Duration: use task `created_at` to `updated_at` delta if `duration_ms` is not in the response

### Step 8: Update Observability Hook

Modify `services/console/src/features/task-detail/useTaskObservability.ts`:
- The hook fetches from the same endpoint (`/v1/tasks/{taskId}/observability`)
- Response type changes to match the simplified schema (no spans, no trace_id)
- Polling behavior remains the same (3s during execution, stop on terminal)

### Step 9: Infrastructure Relocation

Move the Langfuse docker-compose:
- FROM: `infrastructure/local/langfuse/docker-compose.yml`
- TO: `tests/fixtures/langfuse/docker-compose.yml`
- Create `tests/fixtures/langfuse/` directory if needed

Modify `Makefile`:
- Update the `LANGFUSE_COMPOSE_FILE` variable (or equivalent) to point to `tests/fixtures/langfuse/docker-compose.yml`
- Remove `langfuse-up` from the `start` target
- Rename targets:
  - `langfuse-up` → `test-langfuse-up`
  - `langfuse-down` → `test-langfuse-down`
  - `langfuse-status` → `test-langfuse-status`
- Add `dev-langfuse-up`: convenience target that starts the test Langfuse instance for developers who want to test Langfuse integration locally

Modify `.env.localdev.example`:
- Remove mandatory Langfuse environment variables (`LANGFUSE_ENABLED`, `LANGFUSE_HOST`, `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`)
- Add a comment noting that Langfuse is now configured per-agent via the Settings page

### Step 10: Clean Up Integration Test

Modify `tests/backend-integration/test_observability_langfuse.py`:
- Remove the old `config_overrides` approach that passes `langfuse_enabled`, `langfuse_host`, etc. to the worker — these config fields no longer exist after Task 3
- New test flow:
  1. Create a Langfuse endpoint via `POST /v1/langfuse-endpoints` (using the test fixture's local credentials)
  2. Submit a task with `langfuse_endpoint_id` referencing that endpoint
  3. Wait for task completion
  4. Verify the task completed successfully with `cost_microdollars > 0` in the observability response
  5. (Optional) Query the test Langfuse instance directly to verify traces were published
- Remove assertion on `trace_id` (no longer in the observability response)
- Add assertion on checkpoint cost data being populated

### Step 11: Update CI Workflow

Modify `.github/workflows/ci.yml` — the `observability-smoke-tests` job:
- Update the docker compose path: `infrastructure/local/langfuse/docker-compose.yml` → `tests/fixtures/langfuse/docker-compose.yml`
- Remove `LANGFUSE_ENABLED`, `LANGFUSE_HOST`, `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY` from the job's environment variables (the worker no longer reads these)
- The test now creates an endpoint via the API and submits a task referencing it — the worker resolves credentials from the DB, not env vars
- Keep the docker compose startup and wait steps (the test Langfuse instance is still needed as the target for trace publishing)

## Acceptance Criteria
- [ ] Settings page at `/settings` displays and manages Langfuse endpoints (CRUD).
- [ ] "Test Connection" button verifies connectivity to a Langfuse host.
- [ ] Task submission form has optional Langfuse endpoint dropdown.
- [ ] Submitting a task with a selected endpoint stores the `langfuse_endpoint_id`.
- [ ] Task detail shows checkpoint-based execution timeline (no Langfuse spans).
- [ ] CostSummary shows costs from checkpoint data.
- [ ] Docker compose moved to `tests/fixtures/langfuse/`.
- [ ] `make start` no longer starts Langfuse.
- [ ] `make test-langfuse-up` starts the test Langfuse instance.
- [ ] `.env.localdev.example` has no mandatory Langfuse env vars.
- [ ] All existing Console functionality works (dashboard, task list, task detail, dead letter).
- [ ] CI `observability-smoke-tests` job uses updated docker compose path and no Langfuse env vars.
- [ ] Integration test creates endpoint via API and submits task with endpoint reference.

## Testing Requirements
- **Component tests:** Settings page renders, CRUD operations work via mocked API. Task form includes endpoint dropdown.
- **Integration tests:** Full round-trip: create endpoint in settings, submit task with endpoint, verify task detail shows checkpoint costs.
- **Visual verification:** Settings page matches existing Console styling. Task detail looks correct without Langfuse spans.

## Constraints and Guardrails
- Follow existing Console patterns: React Query for data fetching, shadcn/ui for components, Tailwind for styling.
- Do not add new npm dependencies unless absolutely necessary.
- The Console must never make direct HTTP calls to any Langfuse instance.
- Ensure the Settings page is accessible from all viewport sizes.
- Use the same styling patterns as existing pages (e.g., DashboardPage, TaskListPage).
