package com.persistentagent.api.model.response;

import com.fasterxml.jackson.annotation.JsonProperty;
import com.fasterxml.jackson.databind.JsonNode;

import java.time.OffsetDateTime;
import java.util.List;

/**
 * Phase 2 Track 7 Task 13 — a single entry in the user-facing conversation log.
 *
 * <p>{@code content} and {@code metadata} are opaque {@link JsonNode} values:
 * their per-kind shape is documented in the Task 13 spec (§Content schema) but
 * is NOT enforced at the Java layer. This is deliberate — a schema-v2 entry
 * served to a schema-v1 Console must degrade gracefully. Console owns the
 * per-{@code kind} rendering.
 */
public record ConversationEntryResponse(
        @JsonProperty("sequence") long sequence,
        @JsonProperty("kind") String kind,
        @JsonProperty("role") String role,
        @JsonProperty("content_version") int contentVersion,
        @JsonProperty("content") JsonNode content,
        @JsonProperty("metadata") JsonNode metadata,
        @JsonProperty("content_size") int contentSize,
        @JsonProperty("created_at") OffsetDateTime createdAt
) {

    /**
     * Paginated response shape for {@code GET /v1/tasks/{taskId}/conversation}.
     *
     * <p>{@code nextSequence} = max({@link #sequence()}) across {@code entries}
     * when the page is full ({@code len(entries) == limit}), else {@code null}.
     * Clients continue pagination with {@code after_sequence=nextSequence}.
     */
    public record Page(
            @JsonProperty("entries") List<ConversationEntryResponse> entries,
            @JsonProperty("next_sequence") Long nextSequence
    ) {
    }
}
