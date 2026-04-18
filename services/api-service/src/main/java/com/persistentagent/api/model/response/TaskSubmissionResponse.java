package com.persistentagent.api.model.response;

import com.fasterxml.jackson.annotation.JsonProperty;

import java.time.OffsetDateTime;
import java.util.List;
import java.util.UUID;

public record TaskSubmissionResponse(
        @JsonProperty("task_id") UUID taskId,
        @JsonProperty("agent_id") String agentId,
        @JsonProperty("agent_display_name") String agentDisplayName,
        String status,
        @JsonProperty("created_at") OffsetDateTime createdAt,
        /**
         * Attached memory ids in submission order. Always present (possibly {@code []}).
         * Mirrors {@link TaskStatusResponse#attachedMemoryIds()}.
         */
        @JsonProperty("attached_memory_ids") List<UUID> attachedMemoryIds,
        /**
         * Live preview rows (memory_id + title) joined against the scoped memory table.
         * Always present (possibly {@code []}). Entries whose memory no longer resolves
         * (deleted) are silently dropped from the preview but remain in
         * {@link #attachedMemoryIds()}.
         */
        @JsonProperty("attached_memories_preview") List<AttachedMemoryPreview> attachedMemoriesPreview
) {
}
