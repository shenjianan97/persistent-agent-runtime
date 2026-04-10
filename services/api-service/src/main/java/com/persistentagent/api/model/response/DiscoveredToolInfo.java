package com.persistentagent.api.model.response;

import com.fasterxml.jackson.annotation.JsonProperty;

public record DiscoveredToolInfo(
    String name,
    String description,
    @JsonProperty("input_schema") Object inputSchema
) {}
