package com.persistentagent.api.model.response;

public record LangfuseEndpointTestResponse(
        boolean reachable,
        String message
) {
}
