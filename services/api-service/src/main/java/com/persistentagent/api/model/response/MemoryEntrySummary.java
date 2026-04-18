package com.persistentagent.api.model.response;

import com.fasterxml.jackson.annotation.JsonInclude;
import com.fasterxml.jackson.annotation.JsonProperty;

import java.time.OffsetDateTime;

/**
 * Lightweight memory entry summary used by the list endpoint and as the base
 * payload for search results (which add {@code summary_preview} and {@code score}).
 */
@JsonInclude(JsonInclude.Include.NON_NULL)
public record MemoryEntrySummary(
        @JsonProperty("memory_id") String memoryId,
        @JsonProperty("title") String title,
        @JsonProperty("outcome") String outcome,
        @JsonProperty("task_id") String taskId,
        @JsonProperty("created_at") OffsetDateTime createdAt,
        @JsonProperty("summary_preview") String summaryPreview,
        @JsonProperty("score") Double score
) {
    /** Convenience constructor for list responses where preview + score are absent. */
    public static MemoryEntrySummary listItem(
            String memoryId,
            String title,
            String outcome,
            String taskId,
            OffsetDateTime createdAt) {
        return new MemoryEntrySummary(memoryId, title, outcome, taskId, createdAt, null, null);
    }
}
