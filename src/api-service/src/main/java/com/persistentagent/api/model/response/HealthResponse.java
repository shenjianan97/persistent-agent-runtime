package com.persistentagent.api.model.response;

import com.fasterxml.jackson.annotation.JsonProperty;

public record HealthResponse(
        String status,
        String database,
        @JsonProperty("active_workers") int activeWorkers,
        @JsonProperty("queued_tasks") int queuedTasks
) {
}
