package com.persistentagent.api.model.response;

import com.fasterxml.jackson.annotation.JsonProperty;

import java.time.OffsetDateTime;
import java.util.UUID;

public record TaskSubmissionResponse(
        @JsonProperty("task_id") UUID taskId,
        @JsonProperty("agent_id") String agentId,
        @JsonProperty("agent_display_name") String agentDisplayName,
        String status,
        @JsonProperty("created_at") OffsetDateTime createdAt
) {
}
