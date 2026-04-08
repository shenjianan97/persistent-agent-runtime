package com.persistentagent.api.model.request;

import com.fasterxml.jackson.annotation.JsonProperty;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.Pattern;
import jakarta.validation.constraints.Size;

public record ToolServerCreateRequest(
    @NotBlank(message = "name is required")
    @Size(max = 100, message = "name must not exceed 100 characters")
    @Pattern(regexp = "^[a-z0-9][a-z0-9-]*$", message = "name must be lowercase alphanumeric with hyphens, not starting with a hyphen")
    String name,

    @NotBlank(message = "url is required")
    @Size(max = 2048, message = "url must not exceed 2048 characters")
    String url,

    @JsonProperty("auth_type")
    String authType,

    @JsonProperty("auth_token")
    String authToken
) {}
