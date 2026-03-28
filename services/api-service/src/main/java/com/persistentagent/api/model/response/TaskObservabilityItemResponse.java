package com.persistentagent.api.model.response;

import com.fasterxml.jackson.annotation.JsonProperty;

import java.time.OffsetDateTime;

public record TaskObservabilityItemResponse(
        @JsonProperty("item_id") String itemId,
        @JsonProperty("parent_item_id") String parentItemId,
        String kind,
        String title,
        String summary,
        @JsonProperty("step_number") Integer stepNumber,
        @JsonProperty("node_name") String nodeName,
        @JsonProperty("tool_name") String toolName,
        @JsonProperty("model_name") String modelName,
        @JsonProperty("cost_microdollars") long costMicrodollars,
        @JsonProperty("input_tokens") int inputTokens,
        @JsonProperty("output_tokens") int outputTokens,
        @JsonProperty("total_tokens") int totalTokens,
        @JsonProperty("duration_ms") Long durationMs,
        Object input,
        Object output,
        @JsonProperty("started_at") OffsetDateTime startedAt,
        @JsonProperty("ended_at") OffsetDateTime endedAt
) {
}
