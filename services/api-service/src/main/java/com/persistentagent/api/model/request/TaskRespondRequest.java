package com.persistentagent.api.model.request;

import jakarta.validation.constraints.NotBlank;

public record TaskRespondRequest(
        @NotBlank(message = "message is required")
        String message
) {
}
