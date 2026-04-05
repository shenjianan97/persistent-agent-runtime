package com.persistentagent.api.model.request;

import com.fasterxml.jackson.annotation.JsonProperty;
import jakarta.validation.Valid;
import jakarta.validation.constraints.Min;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.NotNull;
import jakarta.validation.constraints.Size;

public record AgentCreateRequest(
        @NotBlank(message = "display_name is required")
        @Size(max = 200, message = "display_name must not exceed 200 characters")
        @JsonProperty("display_name") String displayName,

        @NotNull(message = "agent_config is required")
        @Valid
        @JsonProperty("agent_config") AgentConfigRequest agentConfig,

        @Min(value = 1, message = "max_concurrent_tasks must be at least 1")
        @JsonProperty("max_concurrent_tasks") Integer maxConcurrentTasks,

        @Min(value = 1, message = "budget_max_per_task must be at least 1")
        @JsonProperty("budget_max_per_task") Long budgetMaxPerTask,

        @Min(value = 1, message = "budget_max_per_hour must be at least 1")
        @JsonProperty("budget_max_per_hour") Long budgetMaxPerHour
) {}
