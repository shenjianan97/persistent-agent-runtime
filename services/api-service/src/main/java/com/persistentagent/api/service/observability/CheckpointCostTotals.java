package com.persistentagent.api.service.observability;

public record CheckpointCostTotals(
        long totalCostMicrodollars,
        int inputTokens,
        int outputTokens,
        int totalTokens,
        Long durationMs
) {
    public static CheckpointCostTotals empty() {
        return new CheckpointCostTotals(0L, 0, 0, 0, null);
    }
}
