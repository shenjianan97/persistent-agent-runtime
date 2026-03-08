package com.persistentagent.api.model.response;

import com.fasterxml.jackson.annotation.JsonProperty;

import java.time.OffsetDateTime;
import java.util.UUID;

public record DeadLetterItemResponse(
        @JsonProperty("task_id") UUID taskId,
        @JsonProperty("agent_id") String agentId,
        @JsonProperty("dead_letter_reason") String deadLetterReason,
        @JsonProperty("last_error_code") String lastErrorCode,
        @JsonProperty("last_error_message") String lastErrorMessage,
        @JsonProperty("retry_count") int retryCount,
        @JsonProperty("last_worker_id") String lastWorkerId,
        @JsonProperty("dead_lettered_at") OffsetDateTime deadLetteredAt
) {
}
