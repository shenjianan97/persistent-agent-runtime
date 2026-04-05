package com.persistentagent.api.model.response;

import com.fasterxml.jackson.annotation.JsonProperty;

import java.time.OffsetDateTime;

public record AgentResponse(
        @JsonProperty("agent_id") String agentId,
        @JsonProperty("display_name") String displayName,
        @JsonProperty("agent_config") Object agentConfig,
        String status,
        @JsonProperty("max_concurrent_tasks") int maxConcurrentTasks,
        @JsonProperty("budget_max_per_task") long budgetMaxPerTask,
        @JsonProperty("budget_max_per_hour") long budgetMaxPerHour,
        @JsonProperty("created_at") OffsetDateTime createdAt,
        @JsonProperty("updated_at") OffsetDateTime updatedAt
) {}
