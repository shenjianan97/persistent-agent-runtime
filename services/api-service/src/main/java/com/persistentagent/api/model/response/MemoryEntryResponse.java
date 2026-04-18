package com.persistentagent.api.model.response;

import com.fasterxml.jackson.annotation.JsonInclude;
import com.fasterxml.jackson.annotation.JsonProperty;

import java.time.OffsetDateTime;
import java.util.List;

/**
 * Full memory entry detail — returned by {@code GET /v1/agents/{agent_id}/memory/{memory_id}}.
 */
@JsonInclude(JsonInclude.Include.NON_NULL)
public record MemoryEntryResponse(
        @JsonProperty("memory_id") String memoryId,
        @JsonProperty("agent_id") String agentId,
        @JsonProperty("task_id") String taskId,
        @JsonProperty("title") String title,
        @JsonProperty("summary") String summary,
        @JsonProperty("observations") List<String> observations,
        @JsonProperty("outcome") String outcome,
        @JsonProperty("tags") List<String> tags,
        @JsonProperty("summarizer_model_id") String summarizerModelId,
        @JsonProperty("version") int version,
        @JsonProperty("created_at") OffsetDateTime createdAt,
        @JsonProperty("updated_at") OffsetDateTime updatedAt
) {}
