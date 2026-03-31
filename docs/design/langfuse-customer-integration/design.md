# Customer-Owned Langfuse Integration

## Context

The current Langfuse integration treats Langfuse as platform-owned infrastructure: a single instance started via `make start`, credentials in environment variables, worker fails without it, and the Console queries it to render execution traces. This is architecturally wrong. Langfuse should be a customer-facing integration -- like Prometheus -- where the platform publishes traces to a customer-provided endpoint. The platform's own observability uses CloudWatch (operator-facing) and its own DB for cost/token tracking.

This spec redesigns the integration so that:
1. Customers register their Langfuse endpoints via the Console
2. When creating a task, customers optionally select a Langfuse endpoint
3. The worker publishes traces to the selected endpoint (or skips if none)
4. The platform tracks cost/tokens internally, independent of Langfuse
5. The Console shows platform-owned data only; customers view Langfuse traces in their own Langfuse UI

## Data Model

### New table: `langfuse_endpoints`

Added directly to `infrastructure/database/migrations/0001_phase1_durable_execution.sql` (before the `tasks` table). No separate migration — the system is in development with no production data.

| Column | Type | Constraints |
|--------|------|-------------|
| endpoint_id | UUID | PK, DEFAULT gen_random_uuid() |
| tenant_id | TEXT | NOT NULL |
| name | TEXT | NOT NULL |
| host | TEXT | NOT NULL |
| public_key | TEXT | NOT NULL |
| secret_key | TEXT | NOT NULL |
| created_at | TIMESTAMPTZ | NOT NULL DEFAULT NOW() |
| updated_at | TIMESTAMPTZ | NOT NULL DEFAULT NOW() |

- UNIQUE constraint on `(tenant_id, name)`
- Index on `tenant_id` for list queries

### Tasks table change

`langfuse_endpoint_id UUID REFERENCES langfuse_endpoints(endpoint_id) ON DELETE SET NULL` added directly to the `CREATE TABLE tasks` definition in `0001`. Nullable — tasks without Langfuse are unaffected. `ON DELETE SET NULL` prevents orphaned references if an endpoint is removed.

### Local dev seed

```sql
INSERT INTO langfuse_endpoints (tenant_id, name, host, public_key, secret_key)
VALUES ('default', 'Local Dev', 'http://127.0.0.1:3300', 'pk-lf-local', 'sk-lf-local')
ON CONFLICT (tenant_id, name) DO NOTHING;
```

## API: Langfuse Endpoint Management

New REST resource: `/v1/langfuse-endpoints`

| Method | Path | Status | Description |
|--------|------|--------|-------------|
| POST | `/v1/langfuse-endpoints` | 201 | Create endpoint |
| GET | `/v1/langfuse-endpoints` | 200 | List endpoints for tenant |
| GET | `/v1/langfuse-endpoints/{id}` | 200 | Get one (secrets redacted) |
| PUT | `/v1/langfuse-endpoints/{id}` | 200 | Update endpoint |
| DELETE | `/v1/langfuse-endpoints/{id}` | 204 | Delete (409 if in use by active tasks) |
| POST | `/v1/langfuse-endpoints/{id}/test` | 200 | Test connectivity |

### Request body (create/update)

```json
{
    "name": "Production Langfuse",
    "host": "https://langfuse.mycompany.com",
    "public_key": "pk-lf-...",
    "secret_key": "sk-lf-..."
}
```

### Response body (get/list)

```json
{
    "endpoint_id": "uuid",
    "tenant_id": "default",
    "name": "Production Langfuse",
    "host": "https://langfuse.mycompany.com",
    "created_at": "2026-03-29T...",
    "updated_at": "2026-03-29T..."
}
```

Secrets are never returned in GET responses. The test endpoint returns `{ "reachable": true, "message": "OK" }`.

### Task submission change

`POST /v1/tasks` request body gains an optional field:

```json
{
    "langfuse_endpoint_id": "uuid-of-registered-endpoint"
}
```

If provided, validated that the endpoint exists for the tenant (400 if not). Stored on the task row.

## Worker: Per-Task Langfuse Publishing

### What gets removed

- `WorkerConfig` fields: `langfuse_enabled`, `langfuse_host`, `langfuse_public_key`, `langfuse_secret_key`
- `WorkerConfig.__post_init__` Langfuse validation
- `main.py`: `_assert_langfuse_ready()` function and its call
- `GraphExecutor.__init__`: singleton `self._langfuse_client` and `_initialize_langfuse_client()`

### What gets added

In `GraphExecutor.execute_task()`:

1. Read `langfuse_endpoint_id` from the task row (already available via the claim query)
2. If present, query `langfuse_endpoints` table for `{host, public_key, secret_key}`
3. Create a per-execution `Langfuse` client and `CallbackHandler`
4. Attach callback to the LangGraph runnable config
5. On completion, flush the per-task client in the `finally` block

All Langfuse operations are wrapped in try/except. Failures log a warning; the task continues without traces. Langfuse must never fail a task.

### What gets modified

- `_build_langfuse_callback()`: accepts explicit credentials instead of reading from `self.config`
- `_build_runnable_config()`: accepts optional credentials parameter
- The task claim SQL (in `core/poller.py` or the claim query builder): must SELECT `langfuse_endpoint_id`

## Worker: Restored Internal Cost Tracking

The previous Langfuse integration removed manual cost tracking, making Langfuse the only source of cost data. This created a hard dependency. Cost tracking is restored as platform-owned data.

### Approach

After each LLM response in the agent node:
1. Extract `input_tokens` and `output_tokens` from `response.response_metadata` (LangChain convention)
2. Look up per-model cost rates from the `models` table (`input_microdollars_per_million`, `output_microdollars_per_million`)
3. Calculate `cost_microdollars`
4. Write to the existing checkpoint columns: `cost_microdollars` (INT) and `execution_metadata` (JSONB with `{input_tokens, output_tokens, model}`)

These columns already exist in the schema but were left unused after the Langfuse integration. No migration needed.

### Console impact

`CostSummary.tsx` and the task detail view source their data from the platform DB (checkpoint aggregates), not from Langfuse. This is the existing `findByIdWithAggregates` query pattern -- it just needs to sum `cost_microdollars` from checkpoints.

## API Service: Remove Langfuse Query Code

### What gets removed

- `LangfuseTaskObservabilityService.java` -- the 462-line service that queries customer's Langfuse API. The platform should not access customer's Langfuse instance.
- `TaskObservabilityService.java` interface
- `TaskObservabilityTotals.java`
- Global Langfuse config in `application.yml` (`app.langfuse.*` block)
- `@Value`-injected Langfuse credentials

### What gets modified

- `TaskService.java`: cost/token totals now come from checkpoint aggregation in the DB, not from Langfuse. Modify `getTaskStatus()` and `getTaskObservability()` to query checkpoints.
- `TaskController.java`: the `/v1/tasks/{taskId}/observability` endpoint either gets removed or simplified to return checkpoint-sourced execution timeline (no Langfuse spans).

### Response model changes

- `TaskObservabilityResponse`: items and spans from Langfuse are removed. The response contains only platform-owned data: checkpoint events, cost aggregates, retry markers, completion status.
- `TaskObservabilitySpanResponse`: removed (these were Langfuse spans).
- `TaskObservabilityItemResponse`: kept but simplified to only contain checkpoint/runtime events.

## Console UI

### New: Settings page (`/settings`)

A new route and page for managing Langfuse endpoints:
- Table listing registered endpoints (name, host, created date)
- Add/Edit dialog: name, host, public key, secret key fields + "Test Connection" button
- Delete with confirmation (409 error shown if endpoint is in use)
- Navigation: new "Settings" item in sidebar

### Modified: Task submission form

- Optional "Langfuse Endpoint" dropdown, populated from registered endpoints
- Default: "None" (no Langfuse)
- If no endpoints registered, show a hint: "Configure Langfuse endpoints in Settings"

### Modified: Task detail / ObservabilityTrace

- Rework to show only platform-owned execution data: checkpoint saves, cost per step, retry events, completion/dead-letter markers
- Remove Langfuse span rendering (LLM call details, tool call I/O)
- Cost data sourced from checkpoint `cost_microdollars` aggregation

### Modified: CostSummary

- Source from platform DB checkpoint aggregates instead of Langfuse API
- Same visual presentation (total cost, token counts, per-step cost chart)

## Infrastructure

### Move test fixture

`infrastructure/local/langfuse/docker-compose.yml` -> `tests/fixtures/langfuse/docker-compose.yml`

This signals "test fixture simulating a customer's Langfuse" rather than "platform infrastructure."

### Makefile changes

- Remove `langfuse-up` from the `start` target
- Rename: `langfuse-up` -> `test-langfuse-up`, `langfuse-down` -> `test-langfuse-down`
- Add `dev-langfuse-up`: convenience target that starts the test Langfuse + seeds the default endpoint for local development

## Implementation Phases

### Phase 1: Database Schema

New migration `0005_langfuse_endpoints.sql` with table creation, task column addition, and local dev seed.

**Files:**
- MODIFY: `infrastructure/database/migrations/0001_phase1_durable_execution.sql` — add `langfuse_endpoints` table and `langfuse_endpoint_id` column on `tasks`
- MODIFY: `infrastructure/database/migrations/test_seed.sql` — add local dev seed

**Dependencies:** None. Requires `make db-down && make db-up` to recreate the database.

### Phase 2: API CRUD + Task Submission

New repository, service, controller for Langfuse endpoint management. Wire `langfuse_endpoint_id` through task submission.

**Files:**
- NEW: `services/api-service/src/main/java/com/persistentagent/api/repository/LangfuseEndpointRepository.java`
- NEW: `services/api-service/src/main/java/com/persistentagent/api/model/request/LangfuseEndpointRequest.java`
- NEW: `services/api-service/src/main/java/com/persistentagent/api/model/response/LangfuseEndpointResponse.java`
- NEW: `services/api-service/src/main/java/com/persistentagent/api/model/response/LangfuseEndpointTestResponse.java`
- NEW: `services/api-service/src/main/java/com/persistentagent/api/service/LangfuseEndpointService.java`
- NEW: `services/api-service/src/main/java/com/persistentagent/api/controller/LangfuseEndpointController.java`
- MODIFY: `services/api-service/src/main/java/com/persistentagent/api/model/request/TaskSubmissionRequest.java` -- add optional `langfuse_endpoint_id`
- MODIFY: `services/api-service/src/main/java/com/persistentagent/api/service/TaskService.java` -- validate endpoint on submit
- MODIFY: `services/api-service/src/main/java/com/persistentagent/api/repository/TaskRepository.java` -- add column to INSERT and SELECT

**Dependencies:** Phase 1.

### Phase 3: Worker Per-Task Langfuse + Cost Restore

Remove global Langfuse config, add per-task resolution, restore internal cost tracking.

**Files:**
- MODIFY: `services/worker-service/executor/graph.py` -- per-task Langfuse, restored cost tracking
- MODIFY: `services/worker-service/core/config.py` -- remove Langfuse fields
- MODIFY: `services/worker-service/main.py` -- remove startup assertion
- MODIFY: `services/worker-service/core/poller.py` (or claim query) -- SELECT `langfuse_endpoint_id`

**Dependencies:** Phase 1.

### Phase 4: API Observability Refactor

Remove Langfuse query code from API service. Cost/token data comes from platform DB.

**Files:**
- REMOVE: `services/api-service/src/main/java/com/persistentagent/api/service/observability/LangfuseTaskObservabilityService.java`
- REMOVE: `services/api-service/src/main/java/com/persistentagent/api/service/observability/TaskObservabilityTotals.java`
- MODIFY: `services/api-service/src/main/java/com/persistentagent/api/service/observability/TaskObservabilityService.java` -- simplify interface
- MODIFY: `services/api-service/src/main/java/com/persistentagent/api/service/TaskService.java` -- checkpoint-based cost aggregation
- MODIFY: `services/api-service/src/main/java/com/persistentagent/api/controller/TaskController.java` -- simplify observability endpoint
- MODIFY: `services/api-service/src/main/resources/application.yml` -- remove `app.langfuse` block
- MODIFY: Response models as needed

**Dependencies:** Phase 2 (needs endpoint repository for task detail to show endpoint info), Phase 3 (cost data in checkpoints).

### Phase 5: Console + Infrastructure

Settings page, task form update, Console rework to platform-owned data, infrastructure relocation.

**Files:**
- NEW: `services/console/src/features/settings/SettingsPage.tsx`
- NEW: `services/console/src/features/settings/LangfuseEndpointList.tsx`
- NEW: `services/console/src/features/settings/LangfuseEndpointDialog.tsx`
- NEW: `services/console/src/features/settings/useLangfuseEndpoints.ts`
- MODIFY: `services/console/src/App.tsx` -- add Settings route
- MODIFY: `services/console/src/layout/AppShell.tsx` or sidebar -- add Settings nav item
- MODIFY: `services/console/src/features/submit/SubmitTaskPage.tsx` -- Langfuse dropdown
- MODIFY: `services/console/src/features/task-detail/ObservabilityTrace.tsx` -- platform data only
- MODIFY: `services/console/src/features/task-detail/CostSummary.tsx` -- checkpoint-sourced
- MODIFY: `services/console/src/api/client.ts` -- endpoint CRUD methods
- MOVE: `infrastructure/local/langfuse/docker-compose.yml` -> `tests/fixtures/langfuse/docker-compose.yml`
- MODIFY: `Makefile` -- rename targets, remove from `start`
- MODIFY: `.env.localdev.example` -- remove mandatory Langfuse vars

**Dependencies:** Phases 2-4.

## Phase Dependency Graph

```
Phase 1 (DB migration)
  ├──> Phase 2 (API CRUD + task submission)
  └──> Phase 3 (Worker per-task + cost restore)
       ├──> Phase 4 (API observability refactor)
       └──> Phase 5 (Console + infrastructure)
```

Phases 2 and 3 can be developed in parallel after Phase 1.

## Verification

1. **DB**: Run migration, verify `\d langfuse_endpoints` and `\d tasks` shows new column
2. **API CRUD**: Create/list/update/delete endpoints via curl. Test connectivity against test fixture
3. **Task submission**: Submit task with `langfuse_endpoint_id`, verify it's stored on the task row
4. **Worker (with Langfuse)**: Start test Langfuse (`make test-langfuse-up`), submit task with endpoint configured, verify traces appear in Langfuse UI at `http://127.0.0.1:3300`
5. **Worker (without Langfuse)**: Submit task without endpoint, verify it completes normally with cost data in checkpoints
6. **Worker (Langfuse down)**: Submit task with endpoint pointing to unreachable host, verify task completes with warning logged
7. **Console**: Settings page CRUD works. Task form shows endpoint dropdown. Task detail shows checkpoint-sourced costs. No Langfuse API calls from Console/API service
8. **Regression**: All existing integration tests pass. Tasks without Langfuse behave identically to before
