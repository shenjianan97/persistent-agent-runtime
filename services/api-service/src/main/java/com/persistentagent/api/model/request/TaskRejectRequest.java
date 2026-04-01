package com.persistentagent.api.model.request;

import jakarta.validation.constraints.NotBlank;

public record TaskRejectRequest(
        @NotBlank(message = "reason is required")
        String reason
) {
}
