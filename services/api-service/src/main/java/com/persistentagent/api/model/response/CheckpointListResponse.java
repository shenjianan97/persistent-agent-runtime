package com.persistentagent.api.model.response;

import java.util.List;

public record CheckpointListResponse(
        List<CheckpointResponse> checkpoints
) {
}
