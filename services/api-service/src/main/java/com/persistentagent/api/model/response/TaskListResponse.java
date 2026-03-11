package com.persistentagent.api.model.response;

import java.util.List;

public record TaskListResponse(
        List<TaskSummaryResponse> items
) {
}
