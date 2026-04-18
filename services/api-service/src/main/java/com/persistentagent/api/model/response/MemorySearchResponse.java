package com.persistentagent.api.model.response;

import com.fasterxml.jackson.annotation.JsonProperty;

import java.util.List;

/**
 * Response shape for {@code GET /v1/agents/{agent_id}/memory/search}.
 *
 * <p>{@code ranking_used} is the ranking path actually executed — either the
 * requested mode ({@code hybrid}, {@code text}, or {@code vector}) or
 * {@code "text"} when a hybrid request silently degraded because the
 * embedding provider was unreachable.
 */
public record MemorySearchResponse(
        @JsonProperty("results") List<MemoryEntrySummary> results,
        @JsonProperty("ranking_used") String rankingUsed
) {}
