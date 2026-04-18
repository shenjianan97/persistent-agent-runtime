package com.persistentagent.api.model.response;

import com.fasterxml.jackson.annotation.JsonProperty;

import java.util.UUID;

/**
 * One entry in a task-detail response's {@code attached_memories_preview}. Joined against
 * {@code agent_memory_entries} at read time and scoped to the current
 * {@code (tenant_id, agent_id)}. Memories that no longer resolve (deleted or cross-scope)
 * are omitted from the preview; the full id list in
 * {@link TaskStatusResponse#attachedMemoryIds()} remains complete so the UI can render
 * "N attached memories no longer exist" when the lengths diverge.
 */
public record AttachedMemoryPreview(
        @JsonProperty("memory_id") UUID memoryId,
        String title
) {
}
