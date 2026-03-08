package com.persistentagent.api.model.response;

import com.fasterxml.jackson.annotation.JsonProperty;

import java.util.UUID;

public record RedriveResponse(
        @JsonProperty("task_id") UUID taskId,
        String status
) {
}
