package com.persistentagent.api.model.response;

import com.fasterxml.jackson.annotation.JsonInclude;
import com.fasterxml.jackson.annotation.JsonProperty;

import java.time.OffsetDateTime;
import java.util.List;

/**
 * Phase 2 Track 7 Follow-up Task 8 — unified Activity projection entry.
 *
 * <p>Discriminated-union response shape for
 * {@code GET /v1/tasks/{taskId}/activity}. The {@code kind} field names
 * which other fields carry payload — consumers switch on {@code kind} and
 * ignore fields they don't recognise (forward-compatible with new kinds).
 *
 * <p>Kinds:
 * <ul>
 *   <li>{@code turn.user} / {@code turn.assistant} / {@code turn.tool} —
 *       sourced from {@code checkpoints.checkpoint_payload.channel_values.messages}.
 *       Ordering key: {@code additional_kwargs.emitted_at}, falling back to the
 *       containing checkpoint's {@code created_at}.</li>
 *   <li>{@code marker.compaction_fired} / {@code marker.memory_flush} /
 *       {@code marker.offload_emitted} / {@code marker.system_note} /
 *       {@code marker.lifecycle} / {@code marker.hitl.*} — sourced from
 *       {@code task_events}. Ordering key: {@code created_at}.</li>
 * </ul>
 *
 * <p>Fields not applicable to a given kind are omitted from the JSON via
 * {@link JsonInclude}.
 */
@JsonInclude(JsonInclude.Include.NON_NULL)
public record ActivityEventResponse(
        @JsonProperty("kind") String kind,
        @JsonProperty("timestamp") OffsetDateTime timestamp,
        @JsonProperty("role") String role,
        @JsonProperty("content") String content,
        @JsonProperty("tool_name") String toolName,
        @JsonProperty("tool_call_id") String toolCallId,
        @JsonProperty("tool_calls") List<ToolCall> toolCalls,
        @JsonProperty("is_error") Boolean isError,
        @JsonProperty("event_type") String eventType,
        @JsonProperty("status_before") String statusBefore,
        @JsonProperty("status_after") String statusAfter,
        @JsonProperty("summary_text") String summaryText,
        @JsonProperty("details") Object details
) {

    /** Inline tool-call descriptor on an assistant turn. */
    public record ToolCall(
            @JsonProperty("id") String id,
            @JsonProperty("name") String name,
            @JsonProperty("args") Object args
    ) {}

    /**
     * Paged envelope. v1 returns the entire merged stream in one shot — the
     * {@code next_cursor} field is reserved for future pagination.
     */
    public record Page(
            @JsonProperty("events") List<ActivityEventResponse> events,
            @JsonProperty("next_cursor") String nextCursor
    ) {}
}
