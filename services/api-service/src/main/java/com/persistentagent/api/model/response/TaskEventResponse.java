package com.persistentagent.api.model.response;

import com.fasterxml.jackson.annotation.JsonProperty;

import java.time.OffsetDateTime;
import java.util.UUID;

public record TaskEventResponse(
        @JsonProperty("event_id") UUID eventId,
        @JsonProperty("task_id") UUID taskId,
        @JsonProperty("agent_id") String agentId,
        @JsonProperty("event_type") String eventType,
        @JsonProperty("status_before") String statusBefore,
        @JsonProperty("status_after") String statusAfter,
        @JsonProperty("worker_id") String workerId,
        @JsonProperty("error_code") String errorCode,
        @JsonProperty("error_message") String errorMessage,
        Object details,
        @JsonProperty("created_at") OffsetDateTime createdAt
) {
}
