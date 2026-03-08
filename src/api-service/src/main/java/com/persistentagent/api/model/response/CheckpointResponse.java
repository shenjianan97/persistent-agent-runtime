package com.persistentagent.api.model.response;

import com.fasterxml.jackson.annotation.JsonProperty;

import java.time.OffsetDateTime;

public record CheckpointResponse(
        @JsonProperty("checkpoint_id") String checkpointId,
        @JsonProperty("step_number") int stepNumber,
        @JsonProperty("node_name") String nodeName,
        @JsonProperty("worker_id") String workerId,
        @JsonProperty("cost_microdollars") int costMicrodollars,
        @JsonProperty("execution_metadata") Object executionMetadata,
        @JsonProperty("created_at") OffsetDateTime createdAt
) {
}
