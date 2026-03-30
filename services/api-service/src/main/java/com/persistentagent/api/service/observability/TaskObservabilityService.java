package com.persistentagent.api.service.observability;

import java.util.UUID;

public interface TaskObservabilityService {
    /**
     * Aggregates cost/token totals from checkpoints stored in the platform database.
     */
    CheckpointCostTotals getTaskCostTotals(UUID taskId, String tenantId);
}
