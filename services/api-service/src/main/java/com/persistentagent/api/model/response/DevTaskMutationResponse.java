package com.persistentagent.api.model.response;

import com.fasterxml.jackson.annotation.JsonProperty;

import java.util.UUID;

public record DevTaskMutationResponse(
        @JsonProperty("task_id") UUID taskId,
        String status,
        String message
) {
}
