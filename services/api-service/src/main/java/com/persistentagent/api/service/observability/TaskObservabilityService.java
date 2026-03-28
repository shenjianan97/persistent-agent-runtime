package com.persistentagent.api.service.observability;

import com.persistentagent.api.model.response.TaskObservabilityResponse;

import java.util.UUID;

public interface TaskObservabilityService {
    TaskObservabilityTotals getTaskTotals(UUID taskId, String agentId, String taskStatus);

    TaskObservabilityResponse getTaskObservability(UUID taskId, String agentId, String taskStatus);
}
