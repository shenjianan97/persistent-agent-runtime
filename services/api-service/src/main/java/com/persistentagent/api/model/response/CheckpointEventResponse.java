package com.persistentagent.api.model.response;

import com.fasterxml.jackson.annotation.JsonProperty;

public record CheckpointEventResponse(
        @JsonProperty("type") String type,
        @JsonProperty("title") String title,
        @JsonProperty("summary") String summary,
        @JsonProperty("content") Object content,
        @JsonProperty("tool_name") String toolName,
        @JsonProperty("tool_args") Object toolArgs,
        @JsonProperty("tool_result") Object toolResult,
        @JsonProperty("usage") Object usage
) {
}
