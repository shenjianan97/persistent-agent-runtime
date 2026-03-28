package com.persistentagent.api.service.observability;

public record TaskObservabilityTotals(
        long totalCostMicrodollars,
        int inputTokens,
        int outputTokens,
        int totalTokens,
        Long durationMs,
        String traceId
) {
    public static TaskObservabilityTotals empty() {
        return new TaskObservabilityTotals(0L, 0, 0, 0, null, null);
    }
}
