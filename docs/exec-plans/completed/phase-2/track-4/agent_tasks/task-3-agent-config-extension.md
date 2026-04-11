<!-- AGENT_TASK_START: task-3-agent-config-extension.md -->

# Task 3 — Agent Config Extension: tool_servers Field

## Agent Instructions

You are a software engineer implementing one module of a larger system. Your scope is strictly limited to this task. Do not modify components outside the "Affected Component" listed below.

**CRITICAL PRE-WORK:** Before beginning implementation, you MUST read:
1. `docs/design-docs/phase-2/track-4-custom-tool-runtime.md` — canonical design contract (Agent config extension, API Design sections)
2. `services/api-service/src/main/java/com/persistentagent/api/model/request/AgentConfigRequest.java` — current agent config model
3. `services/api-service/src/main/java/com/persistentagent/api/service/ConfigValidationHelper.java` — current validation logic
4. `services/api-service/src/main/java/com/persistentagent/api/service/AgentService.java` — agent create/update flow
5. `services/api-service/src/main/java/com/persistentagent/api/repository/ToolServerRepository.java` — Task 2 output: `findByTenantAndNames()` method
6. `services/console/src/types/index.ts` — current TypeScript type definitions

**CRITICAL POST-WORK:** After completing this task:
1. Run the full test suite with `make test` and verify all existing tests still pass. Fix any regressions before proceeding.
2. Update the status in `docs/exec-plans/active/phase-2/track-4/progress.md` to "Done".

## Context

Track 4 extends the `agent_config` JSON with a `tool_servers` field — an array of registered server names. When an agent is created or updated, the API validates that each referenced server name exists and is active in the `tool_servers` registry for the tenant. This ensures agents can only reference valid tool servers.

The `allowed_tools` field continues to reference built-in tool names only. `tool_servers` is a separate, orthogonal list.

## Task-Specific Shared Contract

- `tool_servers` is an optional array of strings in `agent_config`. Absent or empty means the agent uses only built-in tools.
- Each name in `tool_servers` must reference an existing, active `tool_servers` row for the tenant.
- Duplicate names in the array are rejected.
- The `tool_servers` field is serialized into the `agent_config` JSONB column alongside existing fields.
- The `tool_servers` field is included in the snapshotted `agent_config` at task submission time (automatic — the snapshot captures the full JSONB).
- Backward compatible: existing agents without `tool_servers` continue to work unchanged.

## Affected Component

- **Service/Module:** API Service — Agent Config Validation
- **File paths:**
  - `services/api-service/src/main/java/com/persistentagent/api/model/request/AgentConfigRequest.java` (modify — add `toolServers` field)
  - `services/api-service/src/main/java/com/persistentagent/api/service/ConfigValidationHelper.java` (modify — add `ToolServerRepository` dependency + `validateToolServers()`)
  - `services/api-service/src/main/java/com/persistentagent/api/service/AgentService.java` (modify — wire up tool server validation)
  - `services/api-service/src/main/java/com/persistentagent/api/config/ValidationConstants.java` (modify — add `TOOL_SERVER_NAME_PATTERN`, `TOOL_SERVER_STATUS_ACTIVE` constants if not already added by Task 2)
  - `services/console/src/types/index.ts` (modify — add `tool_servers` to `AgentConfig` type)
  - `services/api-service/src/test/java/com/persistentagent/api/service/ConfigValidationHelperTest.java` (modify or new — add tool server validation tests)
- **Change type:** modification

## Dependencies

- **Must complete first:** Task 1 (Database Migration), Task 2 (Tool Server API — provides `ToolServerRepository.findByTenantAndNames()`)
- **Provides output to:** Task 5 (Executor Integration — reads `tool_servers` from snapshotted agent config), Task 7 (Console — Agent Config editor)
- **Shared interfaces/contracts:** `agent_config` JSON schema, `AgentConfigRequest` record

## Implementation Specification

### Step 1: Add toolServers field to AgentConfigRequest

Modify `services/api-service/src/main/java/com/persistentagent/api/model/request/AgentConfigRequest.java`:

```java
public record AgentConfigRequest(
    @NotBlank(message = "system_prompt is required")
    @Size(max = 51200, message = "system_prompt must not exceed 50KB")
    @JsonProperty("system_prompt") String systemPrompt,

    @NotBlank(message = "provider is required") String provider,
    @NotBlank(message = "model is required") String model,

    @DecimalMin(value = "0.0", message = "temperature must be >= 0.0")
    @DecimalMax(value = "2.0", message = "temperature must be <= 2.0")
    Double temperature,

    @JsonProperty("allowed_tools") List<String> allowedTools,

    @JsonProperty("tool_servers") List<String> toolServers
) {}
```

The `toolServers` field is optional (nullable). Jackson will deserialize `"tool_servers": ["jira-tools"]` into this field. When absent from the JSON, it defaults to `null`.

### Step 2: Add validateToolServers to ConfigValidationHelper

Modify `services/api-service/src/main/java/com/persistentagent/api/service/ConfigValidationHelper.java`:

```java
import com.persistentagent.api.repository.ToolServerRepository;

@Component
public class ConfigValidationHelper {
    private final ModelRepository modelRepository;
    private final ToolServerRepository toolServerRepository;
    private final Set<String> allowedTools;

    public ConfigValidationHelper(ModelRepository modelRepository,
                                   ToolServerRepository toolServerRepository,
                                   @Value("${app.dev-task-controls.enabled:false}") boolean devTaskControlsEnabled) {
        this.modelRepository = modelRepository;
        this.toolServerRepository = toolServerRepository;

        Set<String> tools = new LinkedHashSet<>(ValidationConstants.ALLOWED_TOOLS);
        if (devTaskControlsEnabled) {
            tools.addAll(ValidationConstants.DEV_TASK_CONTROL_TOOLS);
        }
        this.allowedTools = Set.copyOf(tools);
    }

    // ... existing methods (validateModel, validateAllowedTools) unchanged ...

    public void validateToolServers(List<String> toolServers) {
        if (toolServers == null || toolServers.isEmpty()) {
            return; // No tool servers is valid
        }

        // Check for duplicates
        Set<String> seen = new HashSet<>();
        for (String name : toolServers) {
            if (!seen.add(name)) {
                throw new ValidationException("Duplicate tool server name: " + name);
            }
        }

        // Validate name format
        for (String name : toolServers) {
            if (!name.matches(ValidationConstants.TOOL_SERVER_NAME_PATTERN)) {
                throw new ValidationException("Invalid tool server name: " + name + ". Must match pattern: " + ValidationConstants.TOOL_SERVER_NAME_PATTERN);
            }
        }

        // Check that all referenced servers exist and are active
        String tenantId = ValidationConstants.DEFAULT_TENANT_ID;
        List<Map<String, Object>> found = toolServerRepository.findByTenantAndNames(tenantId, toolServers);

        Set<String> foundNames = new HashSet<>();
        for (Map<String, Object> row : found) {
            String name = (String) row.get("name");
            String status = (String) row.get("status");
            if (!ValidationConstants.TOOL_SERVER_STATUS_ACTIVE.equals(status)) {
                throw new ValidationException("Tool server '" + name + "' is disabled");
            }
            foundNames.add(name);
        }

        for (String name : toolServers) {
            if (!foundNames.contains(name)) {
                throw new ValidationException("Tool server not found: " + name);
            }
        }
    }

    public void validateAgentConfig(AgentConfigRequest config) {
        validateModel(config.provider(), config.model());
        validateAllowedTools(config.allowedTools());
        validateToolServers(config.toolServers());
    }
}
```

### Step 3: Update AgentService canonicalizeConfig

Modify the `canonicalizeConfig` method in `AgentService.java` to handle the new field:

```java
private AgentConfigRequest canonicalizeConfig(AgentConfigRequest config) {
    return new AgentConfigRequest(
        config.systemPrompt(),
        config.provider(),
        config.model(),
        config.temperature() != null ? config.temperature() : ValidationConstants.DEFAULT_TEMPERATURE,
        config.allowedTools() != null ? config.allowedTools() : List.of(),
        config.toolServers() != null ? config.toolServers() : List.of()
    );
}
```

### Step 4: Update TypeScript types

Modify `services/console/src/types/index.ts` to add `tool_servers` to the `AgentConfig` interface:

```typescript
export interface AgentConfig {
    system_prompt: string;
    provider: string;
    model: string;
    temperature: number;
    allowed_tools: string[];
    tool_servers?: string[];
}
```

Also update `AgentCreateRequest` and `AgentUpdateRequest` if they inline the config shape:

```typescript
export interface AgentCreateRequest {
    display_name: string;
    agent_config: Omit<AgentConfig, 'temperature' | 'allowed_tools' | 'tool_servers'> & {
        temperature?: number;
        allowed_tools?: string[];
        tool_servers?: string[];
    };
    max_concurrent_tasks?: number;
    budget_max_per_task?: number;
    budget_max_per_hour?: number;
}

export interface AgentUpdateRequest {
    display_name: string;
    agent_config: Omit<AgentConfig, 'temperature' | 'allowed_tools' | 'tool_servers'> & {
        temperature?: number;
        allowed_tools?: string[];
        tool_servers?: string[];
    };
    status: 'active' | 'disabled';
    max_concurrent_tasks?: number;
    budget_max_per_task?: number;
    budget_max_per_hour?: number;
}
```

### Step 5: Write unit tests

Add tests to `ConfigValidationHelperTest.java`:

- `testValidateToolServers_null_ok` — null tool_servers passes validation
- `testValidateToolServers_empty_ok` — empty list passes validation
- `testValidateToolServers_validNames_ok` — list of valid, existing, active servers passes
- `testValidateToolServers_duplicateName_throws` — `["jira-tools", "jira-tools"]` throws ValidationException
- `testValidateToolServers_invalidNameFormat_throws` — `["UPPERCASE"]` throws ValidationException
- `testValidateToolServers_serverNotFound_throws` — referencing non-existent server throws
- `testValidateToolServers_serverDisabled_throws` — referencing disabled server throws

Mock `ToolServerRepository.findByTenantAndNames()` in these tests.

## Acceptance Criteria

- [ ] `AgentConfigRequest` has a `toolServers` field mapped to JSON `tool_servers`
- [ ] `ConfigValidationHelper.validateToolServers()` validates: no duplicates, valid format, all servers exist and are active
- [ ] `ConfigValidationHelper.validateAgentConfig()` calls `validateToolServers()`
- [ ] `canonicalizeConfig()` defaults `toolServers` to empty list when null
- [ ] `tool_servers` is serialized into `agent_config` JSONB on agent create/update
- [ ] Existing agents without `tool_servers` continue to work (backward compatible)
- [ ] TypeScript `AgentConfig` type includes optional `tool_servers` field
- [ ] All unit tests pass

## Testing Requirements

- **Unit tests:** Test `validateToolServers()` with null, empty, valid, duplicate, invalid format, missing server, and disabled server cases. Mock the `ToolServerRepository`.
- **Integration tests:** Create a tool server, then create an agent referencing it. Verify the agent's `agent_config` JSON includes `tool_servers`. Update agent to reference a non-existent server — expect 400.

## Constraints and Guardrails

- Do not change the `agents` table schema — `tool_servers` lives inside the `agent_config` JSONB column.
- Do not modify the existing `allowed_tools` validation — it continues to validate built-in tool names only.
- Do not add default tool servers — an agent with no `tool_servers` uses only built-in tools.
- The `AgentConfigRequest` record constructor changes from 5 to 6 parameters. Verify all call sites are updated (the `canonicalizeConfig` method and any test code that constructs `AgentConfigRequest` directly).

## Assumptions

- Task 1 has been completed (`tool_servers` table exists).
- Task 2 has been completed (`ToolServerRepository.findByTenantAndNames()` method exists).
- The `ObjectMapper` serialization of `AgentConfigRequest` includes `tool_servers` in the JSON output (Jackson default for record components).
- The `agent_config` JSONB column stores whatever `ObjectMapper` produces — no schema enforcement at the DB level.

<!-- AGENT_TASK_END: task-3-agent-config-extension.md -->
