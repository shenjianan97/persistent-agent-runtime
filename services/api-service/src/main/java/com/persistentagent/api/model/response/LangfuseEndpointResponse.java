package com.persistentagent.api.model.response;

import com.fasterxml.jackson.annotation.JsonProperty;

import java.time.Instant;
import java.util.UUID;

public record LangfuseEndpointResponse(
        @JsonProperty("endpoint_id") UUID endpointId,
        @JsonProperty("tenant_id") String tenantId,
        String name,
        String host,
        @JsonProperty("created_at") Instant createdAt,
        @JsonProperty("updated_at") Instant updatedAt
) {
}
