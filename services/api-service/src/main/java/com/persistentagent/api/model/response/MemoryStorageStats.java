package com.persistentagent.api.model.response;

import com.fasterxml.jackson.annotation.JsonProperty;

/**
 * Per-agent memory storage footprint surfaced on the first page of the list endpoint.
 *
 * <p>{@code entry_count} is an exact count; {@code approx_bytes} is an order-of-magnitude
 * approximation derived from {@code pg_column_size(...)} sums over the agent's rows —
 * see the design doc "Scale and Operational Plan" section for the rationale.
 */
public record MemoryStorageStats(
        @JsonProperty("entry_count") long entryCount,
        @JsonProperty("approx_bytes") long approxBytes
) {}
