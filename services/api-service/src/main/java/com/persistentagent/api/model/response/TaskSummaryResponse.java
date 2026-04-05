package com.persistentagent.api.model.response;

import com.fasterxml.jackson.annotation.JsonInclude;
import com.fasterxml.jackson.annotation.JsonProperty;

import java.time.OffsetDateTime;
import java.util.UUID;

public record TaskSummaryResponse(
        @JsonProperty("task_id") UUID taskId,
        @JsonProperty("agent_id") String agentId,
        @JsonProperty("agent_display_name") String agentDisplayName,
        String status,
        @JsonProperty("retry_count") int retryCount,
        @JsonProperty("checkpoint_count") int checkpointCount,
        @JsonProperty("total_cost_microdollars") long totalCostMicrodollars,
        @JsonProperty("created_at") OffsetDateTime createdAt,
        @JsonProperty("updated_at") OffsetDateTime updatedAt,
        @JsonInclude(JsonInclude.Include.NON_NULL) @JsonProperty("pause_reason") String pauseReason,
        @JsonInclude(JsonInclude.Include.NON_NULL) @JsonProperty("resume_eligible_at") OffsetDateTime resumeEligibleAt
) {
}
