package com.persistentagent.api.model.response;

import com.fasterxml.jackson.annotation.JsonProperty;

import java.util.UUID;

public record TaskCancelResponse(
        @JsonProperty("task_id") UUID taskId,
        String status,
        @JsonProperty("dead_letter_reason") String deadLetterReason
) {
}
