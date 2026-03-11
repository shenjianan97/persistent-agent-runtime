package com.persistentagent.api.model.request;

import com.fasterxml.jackson.annotation.JsonProperty;
import jakarta.validation.Valid;
import jakarta.validation.constraints.Max;
import jakarta.validation.constraints.Min;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.NotNull;
import jakarta.validation.constraints.Size;

public record TaskSubmissionRequest(
        @JsonProperty("tenant_id")
        String tenantId,

        @NotBlank(message = "agent_id is required")
        @Size(max = 64, message = "agent_id must not exceed 64 characters")
        @JsonProperty("agent_id")
        String agentId,

        @NotNull(message = "agent_config is required")
        @Valid
        @JsonProperty("agent_config")
        AgentConfigRequest agentConfig,

        @NotBlank(message = "input is required")
        @Size(max = 102400, message = "input must not exceed 100KB")
        String input,

        @Min(value = 0, message = "max_retries must be >= 0")
        @Max(value = 10, message = "max_retries must be <= 10")
        @JsonProperty("max_retries")
        Integer maxRetries,

        @Min(value = 1, message = "max_steps must be >= 1")
        @Max(value = 1000, message = "max_steps must be <= 1000")
        @JsonProperty("max_steps")
        Integer maxSteps,

        @Min(value = 1, message = "task_timeout_seconds must be >= 1")
        @Max(value = 86400, message = "task_timeout_seconds must be <= 86400")
        @JsonProperty("task_timeout_seconds")
        Integer taskTimeoutSeconds
) {
}
