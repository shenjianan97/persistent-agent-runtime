package com.persistentagent.api.model.response;

import com.fasterxml.jackson.annotation.JsonProperty;
import java.time.OffsetDateTime;

public record ToolServerResponse(
    @JsonProperty("server_id") String serverId,
    @JsonProperty("tenant_id") String tenantId,
    String name,
    String url,
    @JsonProperty("auth_type") String authType,
    @JsonProperty("auth_token") String authToken,
    String status,
    @JsonProperty("created_at") OffsetDateTime createdAt,
    @JsonProperty("updated_at") OffsetDateTime updatedAt
) {}
