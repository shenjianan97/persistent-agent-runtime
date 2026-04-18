package com.persistentagent.api.model.response;

import com.fasterxml.jackson.annotation.JsonInclude;
import com.fasterxml.jackson.annotation.JsonProperty;

import java.util.List;

/**
 * Paginated list response for the memory list endpoint.
 *
 * <p>{@code next_cursor} is absent when the caller has reached the last page.
 * {@code agent_storage_stats} is populated only on the first page to avoid
 * running the stats aggregation on every pagination hop.
 */
@JsonInclude(JsonInclude.Include.NON_NULL)
public record MemoryListResponse(
        @JsonProperty("items") List<MemoryEntrySummary> items,
        @JsonProperty("next_cursor") String nextCursor,
        @JsonProperty("agent_storage_stats") MemoryStorageStats agentStorageStats
) {}
