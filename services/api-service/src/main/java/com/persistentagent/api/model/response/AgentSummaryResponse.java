package com.persistentagent.api.model.response;

import com.fasterxml.jackson.annotation.JsonProperty;

import java.time.OffsetDateTime;

public record AgentSummaryResponse(
        @JsonProperty("agent_id") String agentId,
        @JsonProperty("display_name") String displayName,
        String provider,
        String model,
        String status,
        @JsonProperty("created_at") OffsetDateTime createdAt,
        @JsonProperty("updated_at") OffsetDateTime updatedAt
) {}
