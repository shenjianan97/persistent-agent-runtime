package com.persistentagent.api.model.response;

import com.fasterxml.jackson.annotation.JsonProperty;

public record ModelInfo(
    String provider,
    @JsonProperty("model_id") String modelId,
    @JsonProperty("display_name") String displayName
) {}
