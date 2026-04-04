<!-- AGENT_TASK_START: task-2-agent-crud-api.md -->

# Task 2 — Agent CRUD API

## Agent Instructions

You are a software engineer implementing one module of a larger system. Your scope is strictly limited to this task.

**CRITICAL PRE-WORK:** Before beginning implementation, you MUST read:
1. `docs/design-docs/phase-2/track-1-agent-control-plane.md` — canonical design contract (API Design section)
2. `services/api-service/src/main/java/com/persistentagent/api/controller/LangfuseEndpointController.java` — CRUD pattern template
3. `services/api-service/src/main/java/com/persistentagent/api/service/LangfuseEndpointService.java` — service layer pattern
4. `services/api-service/src/main/java/com/persistentagent/api/repository/LangfuseEndpointRepository.java` — repository pattern template
5. `services/api-service/src/main/java/com/persistentagent/api/service/TaskService.java` — existing `validateModel()` and `validateAllowedTools()` to extract

**CRITICAL POST-WORK:** After completing this task, update the status in `docs/exec-plans/completed/phase-2/track-1/progress.md` to "Done".

## Context

Track 1 requires a new REST resource at `/v1/agents` with POST, GET, GET/{id}, and PUT operations. The implementation follows the existing Langfuse endpoint CRUD pattern: Controller delegates to Service, Service validates and delegates to Repository, Repository uses JdbcTemplate.

Agent config validation must reuse the same model/tool validation logic currently in `TaskService`. This validation logic should be extracted into a shared utility so both `AgentService` and `TaskService` can call it without duplication.

## Task-Specific Shared Contract

- `agent_id` must be validated as a path-safe slug: `^[a-z0-9][a-z0-9_-]{0,63}$` (lowercase alphanumeric start, then alphanumeric/hyphen/underscore, max 64 chars total).
- `display_name` max length is 200 characters.
- List endpoint returns lightweight `AgentSummaryResponse` (no `agent_config`). Detail endpoint returns full `AgentResponse` with config.
- No DELETE endpoint in Track 1.
- Agent statuses are exactly `active` and `disabled`.
- The design doc specifies PUT with full-replacement semantics: client sends all mutable fields.

## Affected Component

- **Service/Module:** API Service (Java Spring Boot)
- **File paths:**
  - `services/api-service/src/main/java/com/persistentagent/api/controller/AgentController.java` (new)
  - `services/api-service/src/main/java/com/persistentagent/api/service/AgentService.java` (new)
  - `services/api-service/src/main/java/com/persistentagent/api/repository/AgentRepository.java` (new)
  - `services/api-service/src/main/java/com/persistentagent/api/model/request/AgentCreateRequest.java` (new)
  - `services/api-service/src/main/java/com/persistentagent/api/model/request/AgentUpdateRequest.java` (new)
  - `services/api-service/src/main/java/com/persistentagent/api/model/response/AgentResponse.java` (new)
  - `services/api-service/src/main/java/com/persistentagent/api/model/response/AgentSummaryResponse.java` (new)
  - `services/api-service/src/main/java/com/persistentagent/api/exception/AgentNotFoundException.java` (new)
  - `services/api-service/src/main/java/com/persistentagent/api/exception/GlobalExceptionHandler.java` (modify — add agent exception handlers)
  - `services/api-service/src/main/java/com/persistentagent/api/config/ValidationConstants.java` (modify — add agent constants)
  - `services/api-service/src/main/java/com/persistentagent/api/service/ConfigValidationHelper.java` (new — extracted shared validation)
  - `services/api-service/src/main/java/com/persistentagent/api/service/TaskService.java` (modify — use ConfigValidationHelper instead of private methods)
- **Change type:** new code + minor modifications

## Dependencies

- **Must complete first:** Task 1 (Database Schema — agents table must exist)
- **Provides output to:** Task 3 (AgentRepository used for task submission resolution), Task 5 (Console consumes these endpoints)
- **Shared interfaces/contracts:** Agent CRUD REST endpoints. Config validation extracted for reuse by Task 3.

## Implementation Specification

### Step 1: Extract shared config validation

Create `ConfigValidationHelper` (or similar) to extract `validateModel()` and `validateAllowedTools()` from `TaskService`:

```java
@Component
public class ConfigValidationHelper {
    private final ModelRepository modelRepository;
    private final boolean devTaskControlsEnabled;

    public void validateModel(String provider, String model) { ... }
    public void validateAllowedTools(List<String> tools) { ... }
    public void validateAgentConfig(AgentConfigRequest config) {
        validateModel(config.provider(), config.model());
        validateAllowedTools(config.allowedTools());
    }
}
```

Update `TaskService` to delegate to `ConfigValidationHelper` instead of its own private methods.

### Step 2: Create AgentRepository

Follow the `LangfuseEndpointRepository` pattern with JdbcTemplate:

- `insert(String tenantId, String agentId, String displayName, String agentConfigJson)` — `INSERT INTO agents ... RETURNING created_at, updated_at`
- `findByIdAndTenant(String tenantId, String agentId)` — returns `Optional<Map<String, Object>>` with all columns including `agent_config`
- `listByTenant(String tenantId, String status, int limit)` — returns `List<Map<String, Object>>` with summary fields only (no `agent_config`). Supports optional `status` filter. Ordered by `created_at DESC`. Extracts `provider` and `model` from `agent_config` JSONB using `agent_config->>'provider'` and `agent_config->>'model'`.
- `update(String tenantId, String agentId, String displayName, String agentConfigJson, String status)` — full replacement, sets `updated_at = NOW()`. Returns boolean (row updated).

### Step 3: Create request models

**AgentCreateRequest:**
```java
public record AgentCreateRequest(
    @NotBlank @Size(max = 64) @Pattern(regexp = "^[a-z0-9][a-z0-9_-]{0,63}$")
    @JsonProperty("agent_id") String agentId,

    @NotBlank @Size(max = 200)
    @JsonProperty("display_name") String displayName,

    @NotNull @Valid
    @JsonProperty("agent_config") AgentConfigRequest agentConfig
) {}
```

**AgentUpdateRequest:**
```java
public record AgentUpdateRequest(
    @NotBlank @Size(max = 200)
    @JsonProperty("display_name") String displayName,

    @NotNull @Valid
    @JsonProperty("agent_config") AgentConfigRequest agentConfig,

    @NotBlank
    String status
) {}
```

Both reuse the existing `AgentConfigRequest` type for the config payload.

### Step 4: Create response models

**AgentResponse** (full detail — used for create, get detail, update responses):
```java
public record AgentResponse(
    @JsonProperty("agent_id") String agentId,
    @JsonProperty("display_name") String displayName,
    @JsonProperty("agent_config") Object agentConfig,
    String status,
    @JsonProperty("created_at") OffsetDateTime createdAt,
    @JsonProperty("updated_at") OffsetDateTime updatedAt
) {}
```

**AgentSummaryResponse** (lightweight — used for list responses):
```java
public record AgentSummaryResponse(
    @JsonProperty("agent_id") String agentId,
    @JsonProperty("display_name") String displayName,
    String provider,
    String model,
    String status,
    @JsonProperty("created_at") OffsetDateTime createdAt,
    @JsonProperty("updated_at") OffsetDateTime updatedAt
) {}
```

### Step 5: Create AgentService

```java
@Service
public class AgentService {
    private final AgentRepository agentRepository;
    private final ConfigValidationHelper configValidationHelper;
    private final ObjectMapper objectMapper;

    public AgentResponse createAgent(AgentCreateRequest request) { ... }
    public AgentResponse getAgent(String agentId) { ... }
    public List<AgentSummaryResponse> listAgents(String status, Integer limit) { ... }
    public AgentResponse updateAgent(String agentId, AgentUpdateRequest request) { ... }
}
```

Key behaviors:
- `createAgent()`: validate config via `ConfigValidationHelper`, **canonicalize config defaults** (see below), serialize config, insert. Catch `DuplicateKeyException` → throw appropriate exception for 409.
- `getAgent()`: find by ID, throw `AgentNotFoundException` if missing. Parse `agent_config` JSONB for response.
- `listAgents()`: validate status filter if provided (must be `active` or `disabled`). Apply limit (default 50, max 200).
- `updateAgent()`: validate config, **canonicalize config defaults** (see below), validate status (`active`/`disabled`), update. Throw `AgentNotFoundException` if not found.

**IMPORTANT — Config canonicalization before persistence:**

Both `createAgent()` and `updateAgent()` must resolve nullable config fields to their canonical defaults before storing. The `AgentConfigRequest` record allows `temperature` and `allowedTools` to be null, but the stored `agent_config` JSONB must never contain null for these fields. This is critical because Task 3's atomic `INSERT...SELECT` snapshots `agents.agent_config` directly into `tasks.agent_config_snapshot` without any default-application step.

Apply defaults before serialization:

```java
AgentConfigRequest canonicalized = new AgentConfigRequest(
    request.agentConfig().systemPrompt(),
    request.agentConfig().provider(),
    request.agentConfig().model(),
    request.agentConfig().temperature() != null
        ? request.agentConfig().temperature()
        : ValidationConstants.DEFAULT_TEMPERATURE,
    request.agentConfig().allowedTools() != null
        ? request.agentConfig().allowedTools()
        : List.of()
);
String agentConfigJson = objectMapper.writeValueAsString(canonicalized);
```

This ensures the stored config is always fully resolved and ready to be snapshotted onto tasks without further processing.

### Step 6: Create AgentController

```java
@RestController
@RequestMapping("/v1/agents")
public class AgentController {
    @PostMapping → 201 Created with AgentResponse
    @GetMapping → 200 with List<AgentSummaryResponse> (params: status, limit)
    @GetMapping("/{agentId}") → 200 with AgentResponse
    @PutMapping("/{agentId}") → 200 with AgentResponse
}
```

### Step 7: Add exception handling

Create `AgentNotFoundException` extending appropriate base. Add handler in `GlobalExceptionHandler`:
- `AgentNotFoundException` → 404
- `DuplicateKeyException` (or a custom `AgentAlreadyExistsException`) → 409

### Step 8: Add constants to ValidationConstants

```java
public static final int DEFAULT_AGENT_LIST_LIMIT = 50;
public static final int MAX_AGENT_LIST_LIMIT = 200;
public static final Set<String> VALID_AGENT_STATUSES = Set.of("active", "disabled");
public static final String AGENT_ID_PATTERN = "^[a-z0-9][a-z0-9_-]{0,63}$";
```

## Acceptance Criteria

- [ ] `POST /v1/agents` creates an agent and returns 201 with full `AgentResponse`
- [ ] `GET /v1/agents` returns lightweight `AgentSummaryResponse` list with optional `?status=` filter and `?limit=`
- [ ] `GET /v1/agents/{agentId}` returns full `AgentResponse` including `agent_config`
- [ ] `PUT /v1/agents/{agentId}` updates all mutable fields and returns updated `AgentResponse`
- [ ] Duplicate `agent_id` on POST returns 409
- [ ] Invalid model/tool in `agent_config` returns 400
- [ ] Invalid `agent_id` format (not matching slug pattern) returns 400
- [ ] Missing agent on GET/PUT returns 404
- [ ] Invalid status value on PUT returns 400
- [ ] Config validation logic is shared between `AgentService` and `TaskService` (no duplication)
- [ ] Stored `agent_config` JSONB never contains null for `temperature` or `allowed_tools` — defaults are applied before persistence
- [ ] Creating an agent with omitted `temperature` stores the default (0.7); with omitted `allowed_tools` stores empty list `[]`

## Testing Requirements

- **Unit tests:** Mock repository and validation helper. Test controller response shapes, service validation edge cases (duplicate, invalid slug, unsupported model, invalid status).
- **Integration tests:** Full CRUD lifecycle against local PostgreSQL: create → list → get → update → verify.
- **Failure scenarios:** Duplicate agent_id (409), invalid slug format, unsupported model, disabled status value accepted, empty tools list accepted.

## Constraints and Guardrails

- Follow the existing `LangfuseEndpoint*` CRUD pattern for consistency.
- Do not add DELETE endpoints.
- Do not add agent statuses beyond `active` and `disabled`.
- Reuse the existing `AgentConfigRequest` model for the config payload — do not create a separate config model.
- Use JdbcTemplate with `Map<String, Object>` return types, matching the repository pattern.

## Assumptions

- The `AgentConfigRequest` record already exists and includes proper Bean Validation annotations for `system_prompt`, `provider`, `model`, `temperature`, and `allowed_tools`.
- The `GlobalExceptionHandler` already handles `ValidationException` → 400 and can be extended for agent-specific exceptions.

<!-- AGENT_TASK_END: task-2-agent-crud-api.md -->
