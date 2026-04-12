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
