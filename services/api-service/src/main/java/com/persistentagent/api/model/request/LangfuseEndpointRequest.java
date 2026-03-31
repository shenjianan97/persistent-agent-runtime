package com.persistentagent.api.model.request;

import com.fasterxml.jackson.annotation.JsonProperty;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.Size;

public record LangfuseEndpointRequest(
        @NotBlank(message = "name is required")
        @Size(max = 128, message = "name must not exceed 128 characters")
        String name,

        @NotBlank(message = "host is required")
        @Size(max = 512, message = "host must not exceed 512 characters")
        String host,

        @NotBlank(message = "public_key is required")
        @Size(max = 256, message = "public_key must not exceed 256 characters")
        @JsonProperty("public_key")
        String publicKey,

        @NotBlank(message = "secret_key is required")
        @Size(max = 256, message = "secret_key must not exceed 256 characters")
        @JsonProperty("secret_key")
        String secretKey
) {
}
