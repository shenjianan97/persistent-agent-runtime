package com.persistentagent.api.model.response;

import com.fasterxml.jackson.annotation.JsonProperty;

import java.time.OffsetDateTime;
import java.util.UUID;

public record TaskSummaryResponse(
        @JsonProperty("task_id") UUID taskId,
        @JsonProperty("agent_id") String agentId,
        String status,
        @JsonProperty("retry_count") int retryCount,
        @JsonProperty("checkpoint_count") int checkpointCount,
        @JsonProperty("total_cost_microdollars") long totalCostMicrodollars,
        @JsonProperty("created_at") OffsetDateTime createdAt,
        @JsonProperty("updated_at") OffsetDateTime updatedAt
) {
}
