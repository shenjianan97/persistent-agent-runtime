package com.persistentagent.api.model.response;

import com.fasterxml.jackson.annotation.JsonProperty;

import java.time.OffsetDateTime;

public record TaskObservabilitySpanResponse(
        @JsonProperty("span_id") String spanId,
        @JsonProperty("parent_span_id") String parentSpanId,
        @JsonProperty("task_id") String taskId,
        @JsonProperty("agent_id") String agentId,
        @JsonProperty("actor_id") String actorId,
        String type,
        @JsonProperty("node_name") String nodeName,
        @JsonProperty("model_name") String modelName,
        @JsonProperty("tool_name") String toolName,
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
