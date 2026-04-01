package com.persistentagent.api.model.request;

import com.fasterxml.jackson.annotation.JsonProperty;
import jakarta.validation.Valid;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.NotNull;
import jakarta.validation.constraints.Size;

public record AgentUpdateRequest(
        @NotBlank(message = "display_name is required")
        @Size(max = 200, message = "display_name must not exceed 200 characters")
        @JsonProperty("display_name") String displayName,

        @NotNull(message = "agent_config is required")
        @Valid
        @JsonProperty("agent_config") AgentConfigRequest agentConfig,

        @NotBlank(message = "status is required")
        String status
) {}
