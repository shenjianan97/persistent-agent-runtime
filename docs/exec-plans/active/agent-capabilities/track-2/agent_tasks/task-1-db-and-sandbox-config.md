<!-- AGENT_TASK_START: task-1-db-and-sandbox-config.md -->

# Task 1 — Database Migration + Agent Sandbox Config Validation

## Agent Instructions

You are a software engineer implementing one module of a larger system. Your scope is strictly limited to this task. Do not modify components outside the "Affected Component" listed below.

**CRITICAL PRE-WORK:** Before beginning implementation, you MUST read:
1. `docs/design-docs/agent-capabilities/design.md` — canonical design contract (Sections 1 and 3: sandbox config, database schema)
2. `docs/exec-plans/active/agent-capabilities/track-2/plan.md` — Track 2 execution plan
3. `infrastructure/database/migrations/0009_artifact_storage.sql` — Track 1 migration (latest existing migration)
4. `infrastructure/database/migrations/0006_runtime_state_model.sql` — dead_letter_reason CHECK constraint pattern
5. `infrastructure/database/migrations/0001_phase1_durable_execution.sql` — tasks table schema and conventions
6. `services/api-service/src/main/java/com/persistentagent/api/service/ConfigValidationHelper.java` — existing config validation pattern
7. `services/api-service/src/main/java/com/persistentagent/api/model/request/AgentConfigRequest.java` — existing agent config request model
8. `services/api-service/src/main/java/com/persistentagent/api/service/AgentService.java` — agent CRUD with config canonicalization

**CRITICAL POST-WORK:** After completing this task:
1. Run the full test suite with `make test` and verify all existing tests still pass. Fix any regressions before proceeding.
2. Update the status in `docs/exec-plans/active/agent-capabilities/track-2/progress.md` to "Done".

## Context

Track 2 introduces E2B sandbox code execution and file input. This foundational task establishes the database schema changes and agent config validation needed by all subsequent tasks.

The migration adds:
- A `sandbox_id` TEXT column on the `tasks` table for storing the E2B sandbox ID (used for crash recovery)
- Two new `dead_letter_reason` values: `sandbox_lost` and `sandbox_provision_failed`

The agent config validation extends the API to accept and validate a `sandbox` configuration block in the agent's `agent_config` JSONB.

## Task-Specific Shared Contract

- Treat `docs/design-docs/agent-capabilities/design.md` Sections 1 and 3 as the canonical contract.
- `sandbox_id` is stored in the `tasks` table (not in LangGraph checkpoint state) so any worker or the reaper can reconnect to or clean up the sandbox.
- `dead_letter_reason` CHECK constraint must be expanded, not replaced. Use the same DROP+ADD pattern from migration `0006`.
- The `sandbox` config block is optional in `agent_config`. When absent, `sandbox.enabled` defaults to `false`.
- Sandbox config validation rules: `enabled` (boolean), `template` (non-empty string, required when enabled), `vcpu` (integer, 1-8, default 2), `memory_mb` (integer, 512-8192, default 2048), `timeout_seconds` (integer, 60-86400, default 3600).
- `sandbox.timeout_seconds` is validated to be a reasonable minimum (>= 60s) at agent config time. The runtime cross-validation (`sandbox.timeout_seconds` >= per-task `task_timeout_seconds`) happens at task submission time in Track 2 Task 6, because `task_timeout_seconds` is per-task, not per-agent.
- Sandbox tools (`sandbox_exec`, `sandbox_read_file`, `sandbox_write_file`, `sandbox_download`) must be added to `ALLOWED_TOOLS` in `ValidationConstants.java` so they can appear in `allowed_tools`. Note: `upload_artifact` is NOT added here — Track 1 handles that independently.

## Affected Component

- **Service/Module:** Database Schema + API Service (Agent Config Validation)
- **File paths:**
  - `infrastructure/database/migrations/0010_sandbox_support.sql` (new)
  - `services/api-service/src/main/java/com/persistentagent/api/model/request/AgentConfigRequest.java` (modify)
  - `services/api-service/src/main/java/com/persistentagent/api/model/request/SandboxConfigRequest.java` (new)
  - `services/api-service/src/main/java/com/persistentagent/api/service/ConfigValidationHelper.java` (modify)
  - `services/api-service/src/main/java/com/persistentagent/api/service/AgentService.java` (modify)
  - `services/api-service/src/main/java/com/persistentagent/api/config/ValidationConstants.java` (modify)
  - `services/api-service/src/test/java/com/persistentagent/api/service/ConfigValidationHelperTest.java` (new or modify)
- **Change type:** new migration + modification

## Dependencies

- **Must complete first:** Track 1 Task 1 (DB Migration — `task_artifacts` table via `0009_artifact_storage.sql`)
- **Provides output to:** Task 2 (Sandbox Provisioner), Task 3 (sandbox_exec), Task 4 (sandbox file tools), Task 5 (sandbox_download), Task 6 (Multipart Submission), Task 7 (Crash Recovery), Task 8 (Console), Task 9 (Integration Tests)
- **Shared interfaces/contracts:** PostgreSQL schema contract for sandbox support; agent config sandbox validation

## Implementation Specification

### Step 1: Create the database migration file

Create `infrastructure/database/migrations/0010_sandbox_support.sql`:

```sql
-- Agent Capabilities Track 2: E2B Sandbox Support
-- Adds sandbox_id column to tasks and extends dead_letter_reason for sandbox failures.

-- Step 1: Add sandbox_id column to tasks table
ALTER TABLE tasks ADD COLUMN sandbox_id TEXT;

-- Step 2: Expand dead_letter_reason CHECK constraint to include sandbox reasons
ALTER TABLE tasks DROP CONSTRAINT tasks_dead_letter_reason_check;
ALTER TABLE tasks ADD CONSTRAINT tasks_dead_letter_reason_check
    CHECK (dead_letter_reason IN (
        'cancelled_by_user',
        'retries_exhausted',
        'task_timeout',
        'non_retryable_error',
        'max_steps_exceeded',
        'human_input_timeout',
        'rejected_by_user',
        'sandbox_lost',
        'sandbox_provision_failed'
    ));

-- Step 3: Table comment for sandbox_id
COMMENT ON COLUMN tasks.sandbox_id IS 'E2B sandbox ID for reconnection on crash recovery. Set when sandbox is provisioned, cleared on task completion.';
```

- `sandbox_id`: TEXT, nullable. Stores the E2B sandbox ID so any worker can reconnect to the sandbox after a crash.
- `sandbox_lost`: dead-letter reason when sandbox has expired during crash recovery.
- `sandbox_provision_failed`: dead-letter reason when E2B API is unreachable after retries.

### Step 2: Create SandboxConfigRequest record

Create `services/api-service/src/main/java/com/persistentagent/api/model/request/SandboxConfigRequest.java`:

```java
package com.persistentagent.api.model.request;

import com.fasterxml.jackson.annotation.JsonProperty;

public record SandboxConfigRequest(
        Boolean enabled,

        String template,

        Integer vcpu,

        @JsonProperty("memory_mb")
        Integer memoryMb,

        @JsonProperty("timeout_seconds")
        Integer timeoutSeconds) {
}
```

### Step 3: Add sandbox field to AgentConfigRequest

Modify `services/api-service/src/main/java/com/persistentagent/api/model/request/AgentConfigRequest.java` to add the sandbox field:

```java
package com.persistentagent.api.model.request;

import com.fasterxml.jackson.annotation.JsonProperty;
import jakarta.validation.constraints.DecimalMax;
import jakarta.validation.constraints.DecimalMin;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.Size;

import java.util.List;

public record AgentConfigRequest(
                @NotBlank(message = "system_prompt is required") @Size(max = 51200, message = "system_prompt must not exceed 50KB") @JsonProperty("system_prompt") String systemPrompt,

                @NotBlank(message = "provider is required") String provider,

                @NotBlank(message = "model is required") String model,

                @DecimalMin(value = "0.0", message = "temperature must be >= 0.0") @DecimalMax(value = "2.0", message = "temperature must be <= 2.0") Double temperature,

                @JsonProperty("allowed_tools") List<String> allowedTools,

                @Size(max = 50, message = "tool_servers must not exceed 50 entries") @JsonProperty("tool_servers") List<String> toolServers,

                SandboxConfigRequest sandbox) {
}
```

### Step 4: Add sandbox tools to ValidationConstants

Modify `services/api-service/src/main/java/com/persistentagent/api/config/ValidationConstants.java` to add sandbox tools and dead-letter reasons:

```java
    /** Stable public tools available in all environments. */
    public static final Set<String> ALLOWED_TOOLS = Set.of(
            "web_search", "read_url", "calculator", "request_human_input",
            "sandbox_exec", "sandbox_read_file", "sandbox_write_file", "sandbox_download");

    /** Allowed dead-letter reasons matching the database constraint. */
    public static final Set<String> ALLOWED_DEAD_LETTER_REASONS = Set.of(
            "cancelled_by_user",
            "retries_exhausted",
            "task_timeout",
            "non_retryable_error",
            "max_steps_exceeded",
            "human_input_timeout",
            "rejected_by_user",
            "sandbox_lost",
            "sandbox_provision_failed"
    );

    // Sandbox config defaults and limits
    public static final int SANDBOX_VCPU_MIN = 1;
    public static final int SANDBOX_VCPU_MAX = 8;
    public static final int SANDBOX_VCPU_DEFAULT = 2;
    public static final int SANDBOX_MEMORY_MB_MIN = 512;
    public static final int SANDBOX_MEMORY_MB_MAX = 8192;
    public static final int SANDBOX_MEMORY_MB_DEFAULT = 2048;
    public static final int SANDBOX_TIMEOUT_SECONDS_MIN = 60;
    public static final int SANDBOX_TIMEOUT_SECONDS_MAX = 86400;
    public static final int SANDBOX_TIMEOUT_SECONDS_DEFAULT = 3600;
```

### Step 5: Add sandbox config validation to ConfigValidationHelper

Modify `services/api-service/src/main/java/com/persistentagent/api/service/ConfigValidationHelper.java` to add `validateSandboxConfig()` and call it from `validateAgentConfig()`:

```java
    public void validateSandboxConfig(SandboxConfigRequest sandbox) {
        if (sandbox == null) {
            return; // No sandbox config is valid — defaults to disabled
        }

        // enabled defaults to false if null
        boolean enabled = sandbox.enabled() != null && sandbox.enabled();

        if (!enabled) {
            return; // Disabled sandbox — no further validation needed
        }

        // template is required when sandbox is enabled
        if (sandbox.template() == null || sandbox.template().isBlank()) {
            throw new ValidationException("sandbox.template is required when sandbox is enabled");
        }

        // vcpu validation
        if (sandbox.vcpu() != null) {
            if (sandbox.vcpu() < ValidationConstants.SANDBOX_VCPU_MIN
                    || sandbox.vcpu() > ValidationConstants.SANDBOX_VCPU_MAX) {
                throw new ValidationException("sandbox.vcpu must be between "
                        + ValidationConstants.SANDBOX_VCPU_MIN + " and "
                        + ValidationConstants.SANDBOX_VCPU_MAX);
            }
        }

        // memory_mb validation
        if (sandbox.memoryMb() != null) {
            if (sandbox.memoryMb() < ValidationConstants.SANDBOX_MEMORY_MB_MIN
                    || sandbox.memoryMb() > ValidationConstants.SANDBOX_MEMORY_MB_MAX) {
                throw new ValidationException("sandbox.memory_mb must be between "
                        + ValidationConstants.SANDBOX_MEMORY_MB_MIN + " and "
                        + ValidationConstants.SANDBOX_MEMORY_MB_MAX);
            }
        }

        // timeout_seconds validation
        if (sandbox.timeoutSeconds() != null) {
            if (sandbox.timeoutSeconds() < ValidationConstants.SANDBOX_TIMEOUT_SECONDS_MIN
                    || sandbox.timeoutSeconds() > ValidationConstants.SANDBOX_TIMEOUT_SECONDS_MAX) {
                throw new ValidationException("sandbox.timeout_seconds must be between "
                        + ValidationConstants.SANDBOX_TIMEOUT_SECONDS_MIN + " and "
                        + ValidationConstants.SANDBOX_TIMEOUT_SECONDS_MAX);
            }
        }

        // Note: sandbox.timeout_seconds is validated here to be a reasonable minimum (>= 60s).
        // The runtime cross-validation (sandbox timeout >= task timeout) happens at task
        // submission time in Track 2 Task 6, because task_timeout_seconds is per-task,
        // not per-agent. At agent config time we can only validate the range.
    }

    public void validateAgentConfig(AgentConfigRequest config) {
        validateModel(config.provider(), config.model());
        validateAllowedTools(config.allowedTools());
        validateToolServers(config.toolServers());
        validateSandboxConfig(config.sandbox());
    }
```

Add the necessary import at the top:

```java
import com.persistentagent.api.model.request.SandboxConfigRequest;
```

### Step 6: Update AgentService canonicalization

Modify `services/api-service/src/main/java/com/persistentagent/api/service/AgentService.java` to canonicalize the sandbox config:

```java
    private AgentConfigRequest canonicalizeConfig(AgentConfigRequest config) {
        SandboxConfigRequest sandbox = config.sandbox();
        SandboxConfigRequest canonicalizedSandbox = null;
        if (sandbox != null) {
            boolean enabled = sandbox.enabled() != null && sandbox.enabled();
            canonicalizedSandbox = new SandboxConfigRequest(
                    enabled,
                    enabled ? sandbox.template() : null,
                    enabled ? (sandbox.vcpu() != null ? sandbox.vcpu() : ValidationConstants.SANDBOX_VCPU_DEFAULT) : null,
                    enabled ? (sandbox.memoryMb() != null ? sandbox.memoryMb() : ValidationConstants.SANDBOX_MEMORY_MB_DEFAULT) : null,
                    enabled ? (sandbox.timeoutSeconds() != null ? sandbox.timeoutSeconds() : ValidationConstants.SANDBOX_TIMEOUT_SECONDS_DEFAULT) : null
            );
        }

        return new AgentConfigRequest(
                config.systemPrompt(),
                config.provider(),
                config.model(),
                config.temperature() != null
                        ? config.temperature()
                        : ValidationConstants.DEFAULT_TEMPERATURE,
                config.allowedTools() != null
                        ? config.allowedTools()
                        : List.of(),
                config.toolServers() != null
                        ? config.toolServers()
                        : List.of(),
                canonicalizedSandbox);
    }
```

Add the necessary import:

```java
import com.persistentagent.api.model.request.SandboxConfigRequest;
```

### Step 7: Write unit tests for sandbox config validation

Create or modify `services/api-service/src/test/java/com/persistentagent/api/service/SandboxConfigValidationTest.java`:

```java
package com.persistentagent.api.service;

import com.persistentagent.api.exception.ValidationException;
import com.persistentagent.api.model.request.SandboxConfigRequest;
import com.persistentagent.api.repository.ModelRepository;
import com.persistentagent.api.repository.ToolServerRepository;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;

import static org.junit.jupiter.api.Assertions.*;

@ExtendWith(MockitoExtension.class)
class SandboxConfigValidationTest {

    @Mock
    private ModelRepository modelRepository;

    @Mock
    private ToolServerRepository toolServerRepository;

    private ConfigValidationHelper helper;

    @BeforeEach
    void setUp() {
        helper = new ConfigValidationHelper(modelRepository, toolServerRepository, false);
    }

    @Test
    void validateSandboxConfig_nullConfig_noError() {
        assertDoesNotThrow(() -> helper.validateSandboxConfig(null));
    }

    @Test
    void validateSandboxConfig_disabledExplicitly_noError() {
        SandboxConfigRequest config = new SandboxConfigRequest(false, null, null, null, null);
        assertDoesNotThrow(() -> helper.validateSandboxConfig(config));
    }

    @Test
    void validateSandboxConfig_enabledNullIsFalse_noError() {
        SandboxConfigRequest config = new SandboxConfigRequest(null, null, null, null, null);
        assertDoesNotThrow(() -> helper.validateSandboxConfig(config));
    }

    @Test
    void validateSandboxConfig_enabledWithValidConfig_noError() {
        SandboxConfigRequest config = new SandboxConfigRequest(true, "python-3.11", 2, 2048, 3600);
        assertDoesNotThrow(() -> helper.validateSandboxConfig(config));
    }

    @Test
    void validateSandboxConfig_enabledWithDefaults_noError() {
        SandboxConfigRequest config = new SandboxConfigRequest(true, "python-3.11", null, null, null);
        assertDoesNotThrow(() -> helper.validateSandboxConfig(config));
    }

    @Test
    void validateSandboxConfig_enabledMissingTemplate_throwsValidation() {
        SandboxConfigRequest config = new SandboxConfigRequest(true, null, 2, 2048, 3600);
        assertThrows(ValidationException.class, () -> helper.validateSandboxConfig(config));
    }

    @Test
    void validateSandboxConfig_enabledBlankTemplate_throwsValidation() {
        SandboxConfigRequest config = new SandboxConfigRequest(true, "  ", 2, 2048, 3600);
        assertThrows(ValidationException.class, () -> helper.validateSandboxConfig(config));
    }

    @Test
    void validateSandboxConfig_vcpuBelowMin_throwsValidation() {
        SandboxConfigRequest config = new SandboxConfigRequest(true, "python-3.11", 0, 2048, 3600);
        assertThrows(ValidationException.class, () -> helper.validateSandboxConfig(config));
    }

    @Test
    void validateSandboxConfig_vcpuAboveMax_throwsValidation() {
        SandboxConfigRequest config = new SandboxConfigRequest(true, "python-3.11", 9, 2048, 3600);
        assertThrows(ValidationException.class, () -> helper.validateSandboxConfig(config));
    }

    @Test
    void validateSandboxConfig_memoryBelowMin_throwsValidation() {
        SandboxConfigRequest config = new SandboxConfigRequest(true, "python-3.11", 2, 256, 3600);
        assertThrows(ValidationException.class, () -> helper.validateSandboxConfig(config));
    }

    @Test
    void validateSandboxConfig_memoryAboveMax_throwsValidation() {
        SandboxConfigRequest config = new SandboxConfigRequest(true, "python-3.11", 2, 16384, 3600);
        assertThrows(ValidationException.class, () -> helper.validateSandboxConfig(config));
    }

    @Test
    void validateSandboxConfig_timeoutBelowMin_throwsValidation() {
        SandboxConfigRequest config = new SandboxConfigRequest(true, "python-3.11", 2, 2048, 30);
        assertThrows(ValidationException.class, () -> helper.validateSandboxConfig(config));
    }

    @Test
    void validateSandboxConfig_timeoutAboveMax_throwsValidation() {
        SandboxConfigRequest config = new SandboxConfigRequest(true, "python-3.11", 2, 2048, 100000);
        assertThrows(ValidationException.class, () -> helper.validateSandboxConfig(config));
    }

    @Test
    void validateSandboxConfig_vcpuBoundaryMin_noError() {
        SandboxConfigRequest config = new SandboxConfigRequest(true, "python-3.11", 1, 2048, 3600);
        assertDoesNotThrow(() -> helper.validateSandboxConfig(config));
    }

    @Test
    void validateSandboxConfig_vcpuBoundaryMax_noError() {
        SandboxConfigRequest config = new SandboxConfigRequest(true, "python-3.11", 8, 2048, 3600);
        assertDoesNotThrow(() -> helper.validateSandboxConfig(config));
    }

    @Test
    void validateSandboxConfig_memoryBoundaryMin_noError() {
        SandboxConfigRequest config = new SandboxConfigRequest(true, "python-3.11", 2, 512, 3600);
        assertDoesNotThrow(() -> helper.validateSandboxConfig(config));
    }

    @Test
    void validateSandboxConfig_memoryBoundaryMax_noError() {
        SandboxConfigRequest config = new SandboxConfigRequest(true, "python-3.11", 2, 8192, 3600);
        assertDoesNotThrow(() -> helper.validateSandboxConfig(config));
    }

    @Test
    void validateSandboxConfig_timeoutBoundaryMin_noError() {
        SandboxConfigRequest config = new SandboxConfigRequest(true, "python-3.11", 2, 2048, 60);
        assertDoesNotThrow(() -> helper.validateSandboxConfig(config));
    }

    @Test
    void validateSandboxConfig_timeoutBoundaryMax_noError() {
        SandboxConfigRequest config = new SandboxConfigRequest(true, "python-3.11", 2, 2048, 86400);
        assertDoesNotThrow(() -> helper.validateSandboxConfig(config));
    }
}
```

## Acceptance Criteria

- [ ] Migration `0010_sandbox_support.sql` applies cleanly on a fresh database after migrations 0001-0009
- [ ] `tasks.sandbox_id` column exists as TEXT, nullable
- [ ] `dead_letter_reason` CHECK constraint includes `sandbox_lost` and `sandbox_provision_failed` in addition to all existing values
- [ ] `SandboxConfigRequest` record exists with `enabled`, `template`, `vcpu`, `memoryMb`, `timeoutSeconds` fields
- [ ] `AgentConfigRequest` includes optional `sandbox` field of type `SandboxConfigRequest`
- [ ] `ConfigValidationHelper.validateSandboxConfig()` validates all sandbox fields with correct ranges
- [ ] `validateSandboxConfig()` is called from `validateAgentConfig()`
- [ ] Sandbox config with `enabled: false` or absent sandbox block passes validation
- [ ] Sandbox config with `enabled: true` requires non-blank `template`
- [ ] `vcpu` must be 1-8 when provided, defaults to 2
- [ ] `memory_mb` must be 512-8192 when provided, defaults to 2048
- [ ] `timeout_seconds` must be 60-86400 when provided, defaults to 3600
- [ ] `AgentService.canonicalizeConfig()` fills sandbox defaults when enabled
- [ ] `ValidationConstants.ALLOWED_TOOLS` includes sandbox tools: `sandbox_exec`, `sandbox_read_file`, `sandbox_write_file`, `sandbox_download`
- [ ] `ValidationConstants.ALLOWED_DEAD_LETTER_REASONS` includes `sandbox_lost` and `sandbox_provision_failed`
- [ ] All unit tests pass for sandbox config validation
- [ ] Existing test seeds still load successfully
- [ ] `make test` passes with no regressions

## Testing Requirements

- **Unit tests:** All validation rules for sandbox config — null config, disabled, enabled with valid config, enabled with missing template, all boundary conditions for vcpu/memory_mb/timeout_seconds, canonicalization defaults.
- **Integration tests:** Apply all migrations 0001-0010 in sequence on a fresh PostgreSQL container. Verify `sandbox_id` column exists on tasks. Verify new dead-letter reasons are accepted by the CHECK constraint.
- **Failure scenarios:** Sandbox enabled with blank template must fail validation. Out-of-range vcpu/memory_mb/timeout_seconds must fail. Dead-letter with `sandbox_lost` or `sandbox_provision_failed` must be accepted by the DB constraint.

## Constraints and Guardrails

- Do not modify existing migration files (0001-0009). All schema changes go in `0010_sandbox_support.sql`.
- Do not add sandbox provisioning, tool registration, or worker-side logic — this task is schema and API validation only.
- Do not change the `tasks` table status CHECK constraint — only `dead_letter_reason`.
- Use `-- Step N:` comment headers in the migration file to match the convention in previous migrations.
- Preserve the existing agent config fields (systemPrompt, provider, model, temperature, allowedTools, toolServers) unchanged.
- The `sandbox` field must be optional and nullable in `AgentConfigRequest`.

## Assumptions

- The migration runs after `0009_artifact_storage.sql` (Track 1) has been applied.
- The naming convention `^\d{4}_.*\.sql$` is followed for automatic pickup by the schema-bootstrap ledger.
- The current `dead_letter_reason` CHECK constraint (from migration `0006`) allows: `cancelled_by_user`, `retries_exhausted`, `task_timeout`, `non_retryable_error`, `max_steps_exceeded`, `human_input_timeout`, `rejected_by_user`.
- Jackson will deserialize the `sandbox` JSON object to `SandboxConfigRequest` automatically via the `AgentConfigRequest` record.
- Track 1 handles adding its own `upload_artifact` tool to `ALLOWED_TOOLS` (since Track 1 must be independently deployable). Track 2 only adds sandbox tool names.

<!-- AGENT_TASK_END: task-1-db-and-sandbox-config.md -->
