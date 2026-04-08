package com.persistentagent.api.model.response;

import com.fasterxml.jackson.annotation.JsonProperty;
import java.util.List;

public record ToolDiscoverResponse(
    @JsonProperty("server_id") String serverId,
    @JsonProperty("server_name") String serverName,
    String status,
    String error,
    List<DiscoveredToolInfo> tools
) {}
