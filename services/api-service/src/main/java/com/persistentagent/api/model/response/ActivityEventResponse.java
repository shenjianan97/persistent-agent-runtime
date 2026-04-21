package com.persistentagent.api.model.response;

import com.fasterxml.jackson.annotation.JsonInclude;
import com.fasterxml.jackson.annotation.JsonProperty;

import java.time.OffsetDateTime;
import java.util.List;
import java.util.Map;

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
 *
 * <p>Two diagnostic fields restored from the legacy panes:
 * <ul>
 *   <li>{@code worker_id} — id of the worker that produced the checkpoint
 *       where this message first appeared. Successive turns with differing
 *       worker ids mark a lease-expiry reclaim / handoff.</li>
 *   <li>{@code orig_bytes} — pre-truncation byte count on {@code turn.tool}
 *       events, set when the worker truncated a large tool output.</li>
 * </ul>
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
        @JsonProperty("details") Object details,
        // Per-turn cost & token usage on `turn.assistant` events — sourced
        // from the AIMessage's `usage_metadata` (tokens) and the checkpoint
        // that first materialised the message (cost). Null on other kinds.
        @JsonProperty("usage") Map<String, Integer> usage,
        @JsonProperty("cost_microdollars") Long costMicrodollars,
        // Worker that produced the checkpoint where this message first
        // materialised. Used by the Console to render worker-handoff banners.
        @JsonProperty("worker_id") String workerId,
        // Pre-truncation byte count on `turn.tool` events when the worker
        // truncated a large tool output. Null on other kinds / untruncated.
        @JsonProperty("orig_bytes") Long origBytes
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
     *
     * <p>{@code truncated} is {@code true} when the server hard-capped the
     * events list (at {@code MAX_EVENTS}) because the underlying stream had
     * more events; {@code null}/{@code false} when the full stream fit.
     */
    public record Page(
            @JsonProperty("events") List<ActivityEventResponse> events,
            @JsonProperty("next_cursor") String nextCursor,
            @JsonProperty("truncated") Boolean truncated
    ) {}
}
