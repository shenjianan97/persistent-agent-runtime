<!-- AGENT_TASK_START: task-2-api-crud.md -->

# Task 2: API CRUD Endpoints + Task Submission Wiring

## Agent Instructions
You are a software engineer implementing one module of a larger system.
Your scope is strictly limited to this task. Do not modify components outside
the "Affected Component" listed below.

**CRITICAL PRE-WORK:** Before beginning implementation, you MUST read the following context files:
1. `docs/design/langfuse-customer-integration/design.md`
2. `services/api-service/src/main/java/com/persistentagent/api/repository/TaskRepository.java` (existing repository pattern)
3. `services/api-service/src/main/java/com/persistentagent/api/controller/TaskController.java` (existing controller pattern)
4. `services/api-service/src/main/java/com/persistentagent/api/model/request/TaskSubmissionRequest.java` (existing request model)
5. `services/api-service/src/main/java/com/persistentagent/api/service/TaskService.java` (task submission flow)

## Context
Customers need to register their Langfuse endpoints via the API, and optionally select one when creating a task. This task builds the full CRUD REST resource for endpoint management and wires the `langfuse_endpoint_id` through task submission.

## Task-Specific Shared Contract
- REST resource at `/v1/langfuse-endpoints` with standard CRUD + connectivity test.
- Secrets (`public_key`, `secret_key`) are never returned in GET responses.
- Delete returns 409 Conflict if the endpoint is referenced by active (queued/running) tasks.
- Task submission accepts optional `langfuse_endpoint_id`; if provided, validated against `langfuse_endpoints` for the tenant.
- Follow existing code patterns: `JdbcTemplate` for repository, `@RestController` for controller, records for request/response models.

## Affected Component
- **Service/Module:** API Service — Langfuse Endpoint Management
- **File paths:** `services/api-service/src/main/java/com/persistentagent/api/`
- **Change type:** new code + modification

## Dependencies
- **Must complete first:** Task 1 (database schema)
- **Provides output to:** Tasks 4, 5
- **Shared interfaces/contracts:** `/v1/langfuse-endpoints` REST API, `langfuse_endpoint_id` on task submission and status responses.

## Implementation Specification

### Step 1: Repository

Create `LangfuseEndpointRepository.java` using `JdbcTemplate` (matching `TaskRepository` pattern):
- `insert(tenantId, name, host, publicKey, secretKey) -> Map<String, Object>` — returns `endpoint_id`, `created_at`
- `findByIdAndTenant(endpointId, tenantId) -> Optional<Map<String, Object>>` — returns all columns
- `listByTenant(tenantId) -> List<Map<String, Object>>` — ordered by `created_at DESC`
- `update(endpointId, tenantId, name, host, publicKey, secretKey) -> boolean` — sets `updated_at = NOW()`
- `delete(endpointId, tenantId) -> boolean`
- `isReferencedByActiveTask(endpointId) -> boolean` — checks if any task with `status IN ('queued', 'running')` references this endpoint

### Step 2: Request/Response Models

Create `LangfuseEndpointRequest.java` — record with Bean Validation:
- `@NotBlank name` (max 128 chars)
- `@NotBlank host` (max 512 chars, should be a valid URL)
- `@NotBlank @JsonProperty("public_key") publicKey` (max 256 chars)
- `@NotBlank @JsonProperty("secret_key") secretKey` (max 256 chars)

Create `LangfuseEndpointResponse.java` — record with:
- `@JsonProperty("endpoint_id") UUID endpointId`
- `@JsonProperty("tenant_id") String tenantId`
- `String name`
- `String host`
- `@JsonProperty("created_at") Instant createdAt`
- `@JsonProperty("updated_at") Instant updatedAt`
- **No secrets** — `public_key` and `secret_key` are never returned

Create `LangfuseEndpointTestResponse.java` — record with:
- `boolean reachable`
- `String message`

### Step 3: Service Layer

Create `LangfuseEndpointService.java` as a `@Service`:
- `create(tenantId, request)` — delegates to repository, handles UNIQUE constraint violation (409 Conflict)
- `list(tenantId)` — delegates to repository
- `get(endpointId, tenantId)` — delegates to repository, 404 if not found
- `update(endpointId, tenantId, request)` — delegates to repository, 404 if not found
- `delete(endpointId, tenantId)` — checks `isReferencedByActiveTask`, returns 409 if in use, otherwise deletes
- `testConnectivity(endpointId, tenantId)` — resolves credentials from DB, tests the Langfuse instance:
  - Target URL: `{host}/api/public/health` (Langfuse's public health endpoint)
  - HTTP method: GET with Basic auth header (`publicKey:secretKey` base64-encoded)
  - Timeout: 5 seconds
  - Success: HTTP 2xx → `{ "reachable": true, "message": "OK" }`
  - Auth failure: HTTP 401/403 → `{ "reachable": false, "message": "Authentication failed — check public key and secret key" }`
  - Unreachable: timeout/connection error → `{ "reachable": false, "message": "Cannot reach host — check URL" }`
  - Other HTTP errors: → `{ "reachable": false, "message": "Unexpected status: {code}" }`

### Step 4: Controller

Create `LangfuseEndpointController.java` as `@RestController` at `/v1/langfuse-endpoints`:
- `POST /` — create (201 Created)
- `GET /` — list for tenant (200)
- `GET /{endpointId}` — get one (200, 404)
- `PUT /{endpointId}` — update (200, 404)
- `DELETE /{endpointId}` — delete (204, 404, 409)
- `POST /{endpointId}/test` — test connectivity (200)

Tenant ID resolution: use the same pattern as `TaskController` (hardcoded `"default"` in Phase 1, ready for header-based resolution in Phase 2).

### Step 5: Task Submission Wiring

Modify `TaskSubmissionRequest.java`:
- Add optional field: `@JsonProperty("langfuse_endpoint_id") UUID langfuseEndpointId`

Modify `TaskService.java` in `submitTask()`:
- If `langfuseEndpointId` is non-null, call `LangfuseEndpointRepository.findByIdAndTenant()` to validate it exists for the tenant
- Throw `ValidationException` / return 400 if endpoint not found

Modify `TaskRepository.java`:
- `insertTask()`: add `langfuseEndpointId` parameter (nullable UUID), extend INSERT SQL to include `langfuse_endpoint_id`
- `findByIdAndTenant()`: add `langfuse_endpoint_id` to SELECT
- `findByIdWithAggregates()`: add `t.langfuse_endpoint_id` to SELECT
- `listTasks()`: add `t.langfuse_endpoint_id` to SELECT

Modify `TaskStatusResponse.java`:
- Add `@JsonProperty("langfuse_endpoint_id") UUID langfuseEndpointId`

## Acceptance Criteria
- [ ] All 6 REST endpoints work correctly (create, list, get, update, delete, test connectivity).
- [ ] Secrets are never returned in GET responses.
- [ ] Delete returns 409 when endpoint is referenced by active tasks.
- [ ] UNIQUE constraint violation on create returns appropriate error.
- [ ] Test connectivity makes HTTP request to Langfuse host with Basic auth.
- [ ] Task submission with valid `langfuse_endpoint_id` stores it on the task row.
- [ ] Task submission with invalid `langfuse_endpoint_id` returns 400.
- [ ] Task submission without `langfuse_endpoint_id` works as before (null).
- [ ] Task status and list responses include `langfuse_endpoint_id`.

## Testing Requirements
- **Unit tests:** Controller tests with mocked service for each endpoint. Service tests for validation logic and delete guard.
- **Integration tests:** Full round-trip: create endpoint, submit task with endpoint, verify task row has FK, delete endpoint (blocked then allowed).
- **Failure scenarios:** Duplicate name (409), invalid endpoint ID on task submit (400), delete in-use endpoint (409), test connectivity to unreachable host.

## Constraints and Guardrails
- Follow existing code patterns exactly (JdbcTemplate, records, @Valid, constructor injection).
- No new dependencies — use existing `java.net.http.HttpClient` for connectivity test.
- Secrets must never appear in logs or API responses.
