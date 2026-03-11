package com.persistentagent.api.model.request;

import com.fasterxml.jackson.annotation.JsonProperty;

public record DevForceDeadLetterRequest(
        String reason,
        @JsonProperty("error_code") String errorCode,
        @JsonProperty("error_message") String errorMessage,
        @JsonProperty("last_worker_id") String lastWorkerId
) {
}
