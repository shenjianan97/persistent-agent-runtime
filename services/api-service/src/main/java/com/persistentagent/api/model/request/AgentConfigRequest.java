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

                @JsonProperty("tool_servers") List<String> toolServers) {
}
