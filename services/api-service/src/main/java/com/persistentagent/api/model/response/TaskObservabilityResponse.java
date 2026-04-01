package com.persistentagent.api.model.response;

import com.fasterxml.jackson.annotation.JsonProperty;

import java.util.List;
import java.util.UUID;

public record TaskObservabilityResponse(
        boolean enabled,
        @JsonProperty("task_id") UUID taskId,
        @JsonProperty("agent_id") String agentId,
        @JsonProperty("agent_display_name") String agentDisplayName,
        String status,
        @JsonProperty("total_cost_microdollars") long totalCostMicrodollars,
        @JsonProperty("input_tokens") int inputTokens,
        @JsonProperty("output_tokens") int outputTokens,
        @JsonProperty("total_tokens") int totalTokens,
        @JsonProperty("duration_ms") Long durationMs,
        List<TaskObservabilityItemResponse> items
) {
}
