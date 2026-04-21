package com.persistentagent.api.service;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.persistentagent.api.config.ValidationConstants;
import com.persistentagent.api.exception.TaskNotFoundException;
import com.persistentagent.api.model.response.ActivityEventResponse;
import com.persistentagent.api.model.response.TaskEventResponse;
import com.persistentagent.api.repository.TaskEventRepository;
import com.persistentagent.api.repository.TaskRepository;
import com.persistentagent.api.util.DateTimeUtil;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Service;

import java.sql.Timestamp;
import java.time.OffsetDateTime;
import java.time.format.DateTimeParseException;
import java.util.ArrayList;
import java.util.Collections;
import java.util.Comparator;
import java.util.HashMap;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.UUID;

/**
 * Phase 2 Track 7 Follow-up Task 8 — unified Conversation + Timeline projection.
 *
 * <p>Collapses the two legacy task-detail views (Console "Conversation" tab
 * backed by {@code task_conversation_log} + "Execution Timeline" tab backed
 * by {@code checkpoints}) into a single on-demand projection over
 * {@code checkpoints} (turns) + {@code task_events} (markers).
 *
 * <p>The final rendered surface carries the per-assistant-turn token usage
 * (from each AIMessage's {@code usage_metadata}) and the per-assistant-turn
 * cost (by walking the full checkpoint list and attributing each non-zero
 * {@code cost_microdollars} to the AIMessage id that first appeared in that
 * checkpoint). The per-turn attribution is what the deprecated Execution
 * Timeline used to surface; carrying it forward is non-negotiable for
 * operators who need to see which turn was expensive.
 */
@Service
public class ActivityProjectionService {

    private static final Logger log = LoggerFactory.getLogger(ActivityProjectionService.class);

    /** Hard cap on merged events returned per request. Prevents runaway
     *  payloads on tasks with >O(10⁴) turns before pagination ships. */
    public static final int MAX_EVENTS = 2_000;

    /** Marker kinds that stay visible when {@code include_details=false}.
     *  These represent user-meaningful events, not infrastructure
     *  telemetry. */
    private static final Set<String> USER_VISIBLE_MARKERS = Set.of(
            "marker.compaction_fired",
            "marker.hitl.paused",
            "marker.hitl.approval_requested",
            "marker.hitl.input_requested",
            "marker.hitl.approved",
            "marker.hitl.rejected",
            "marker.hitl.input_received"
    );

    private final TaskRepository taskRepository;
    private final TaskEventRepository taskEventRepository;
    private final ObjectMapper objectMapper;

    public ActivityProjectionService(
            TaskRepository taskRepository,
            TaskEventRepository taskEventRepository,
            ObjectMapper objectMapper) {
        this.taskRepository = taskRepository;
        this.taskEventRepository = taskEventRepository;
        this.objectMapper = objectMapper;
    }

    public ActivityEventResponse.Page getActivity(UUID taskId, boolean includeDetails) {
        String tenantId = ValidationConstants.DEFAULT_TENANT_ID;

        // 404 on missing-or-foreign-tenant (indistinguishable — no enumeration oracle).
        var checkpoint = taskRepository.getLatestRootCheckpoint(taskId, tenantId);
        List<TaskEventResponse> markerRows = taskEventRepository.listEvents(taskId, tenantId, MAX_EVENTS);
        // The tenant check inside getLatestRootCheckpoint validates the task
        // row exists. A completed task may have no checkpoints (dead-letter
        // before first super-step); verify task existence independently if
        // the checkpoint is absent and no task_events exist either.
        if (checkpoint.isEmpty() && markerRows.isEmpty()) {
            if (taskRepository.findByIdAndTenant(taskId, tenantId).isEmpty()) {
                throw new TaskNotFoundException(taskId);
            }
        }

        // Build cost-attribution map up-front by walking every checkpoint for
        // the task. The map is ai_message_id → cost_microdollars; only
        // checkpoints whose cost is >0 produce entries, and only for AI
        // messages that weren't present in any earlier checkpoint.
        Map<String, Long> costByAiMessageId = buildCostAttributionMap(taskId, tenantId);

        List<ActivityEventResponse> events = new ArrayList<>();

        OffsetDateTime checkpointCreatedAt = null;
        if (checkpoint.isPresent()) {
            Map<String, Object> row = checkpoint.get();
            Object createdAt = row.get("created_at");
            if (createdAt instanceof Timestamp ts) {
                checkpointCreatedAt = DateTimeUtil.toOffsetDateTime(ts);
            }
            Object payload = row.get("checkpoint_payload");
            events.addAll(extractTurns(payload, checkpointCreatedAt, costByAiMessageId));
        }

        for (TaskEventResponse marker : markerRows) {
            ActivityEventResponse mapped = mapMarker(marker);
            if (mapped == null) {
                continue;
            }
            if (!includeDetails && !USER_VISIBLE_MARKERS.contains(mapped.kind())) {
                continue;
            }
            events.add(mapped);
        }

        // Stable sort by timestamp. Turn timestamps fall back to the
        // containing checkpoint's created_at when emitted_at is absent —
        // this produces a coarse but monotone ordering for historical
        // tasks.
        events.sort(Comparator.comparing(ActivityEventResponse::timestamp,
                Comparator.nullsLast(Comparator.naturalOrder())));

        if (events.size() > MAX_EVENTS) {
            events = events.subList(0, MAX_EVENTS);
        }

        return new ActivityEventResponse.Page(events, null);
    }

    // ---------------------------------------------------------------------
    // Per-turn cost attribution — walks all checkpoints in order, threads a
    // running "already-seen AI message IDs" set across them, and assigns each
    // checkpoint's cost to the first AI message that appears. Checkpoints
    // with cost_microdollars == 0 are skipped entirely. Parsing every
    // payload is not cheap but the checkpoint count per task stays O(100s)
    // in practice; we revisit this when tasks routinely exceed that.
    // ---------------------------------------------------------------------

    @SuppressWarnings("unchecked")
    private Map<String, Long> buildCostAttributionMap(UUID taskId, String tenantId) {
        var all = taskRepository.getCheckpoints(taskId, tenantId).orElse(Collections.emptyList());
        if (all.isEmpty()) {
            return Collections.emptyMap();
        }
        Map<String, Long> out = new HashMap<>();
        Set<String> seen = new HashSet<>();
        for (Map<String, Object> row : all) {
            Object costObj = row.get("cost_microdollars");
            long cost = 0;
            if (costObj instanceof Number n) {
                cost = n.longValue();
            }
            Object payload = row.get("checkpoint_payload");
            Map<String, Object> parsed = parsePayload(payload);
            if (parsed == null) {
                continue;
            }
            Object channelValues = parsed.get("channel_values");
            if (!(channelValues instanceof Map<?, ?> channelMap)) {
                continue;
            }
            Object messages = ((Map<String, Object>) channelMap).get("messages");
            if (!(messages instanceof List<?> messageList)) {
                continue;
            }
            String firstNewAiId = null;
            for (Object rawMessage : messageList) {
                if (!(rawMessage instanceof Map<?, ?> messageWrapper)) {
                    continue;
                }
                Object rawKwargs = ((Map<String, Object>) messageWrapper).get("kwargs");
                if (!(rawKwargs instanceof Map<?, ?> kwargsMap)) {
                    continue;
                }
                Map<String, Object> kwargs = (Map<String, Object>) kwargsMap;
                String type = asString(kwargs.get("type"));
                String id = asString(kwargs.get("id"));
                if (id == null || id.isBlank()) {
                    continue;
                }
                if ("ai".equals(type) && !seen.contains(id)) {
                    if (firstNewAiId == null) {
                        firstNewAiId = id;
                    }
                    seen.add(id);
                }
            }
            if (cost > 0 && firstNewAiId != null) {
                out.merge(firstNewAiId, cost, Long::sum);
            }
        }
        return out;
    }

    // ---------------------------------------------------------------------
    // Turn extraction from checkpoint_payload.channel_values.messages
    // ---------------------------------------------------------------------

    @SuppressWarnings("unchecked")
    private List<ActivityEventResponse> extractTurns(
            Object payload,
            OffsetDateTime fallbackTs,
            Map<String, Long> costByAiMessageId) {
        List<ActivityEventResponse> turns = new ArrayList<>();
        Map<String, Object> parsed = parsePayload(payload);
        if (parsed == null) {
            return turns;
        }
        Object channelValues = parsed.get("channel_values");
        if (!(channelValues instanceof Map<?, ?> channelMap)) {
            return turns;
        }
        Object messages = ((Map<String, Object>) channelMap).get("messages");
        if (!(messages instanceof List<?> messageList)) {
            return turns;
        }
        for (Object rawMessage : messageList) {
            if (!(rawMessage instanceof Map<?, ?> messageWrapper)) {
                continue;
            }
            // LangGraph's ``langchain_dumps`` wraps every message in
            // {lc, type: "constructor", id: [...], kwargs: {...}}. The
            // interesting fields live inside ``kwargs``.
            Object rawKwargs = ((Map<String, Object>) messageWrapper).get("kwargs");
            if (!(rawKwargs instanceof Map<?, ?> kwargsMap)) {
                continue;
            }
            Map<String, Object> kwargs = (Map<String, Object>) kwargsMap;
            String type = asString(kwargs.get("type"));
            if (type == null || type.isBlank()) {
                continue;
            }

            OffsetDateTime timestamp = readEmittedAt(kwargs);
            if (timestamp == null) {
                timestamp = fallbackTs;
            }

            switch (type) {
                case "human" -> turns.add(new ActivityEventResponse(
                        "turn.user",
                        timestamp,
                        "user",
                        asString(kwargs.get("content")),
                        null, null, null, null,
                        null, null, null, null, null,
                        null, null));
                case "ai" -> turns.add(buildAssistantTurn(kwargs, timestamp, costByAiMessageId));
                case "tool" -> turns.add(new ActivityEventResponse(
                        "turn.tool",
                        timestamp,
                        "tool",
                        asString(kwargs.get("content")),
                        asString(kwargs.get("name")),
                        asString(kwargs.get("tool_call_id")),
                        null,
                        "error".equalsIgnoreCase(asString(kwargs.get("status"))),
                        null, null, null, null, null,
                        null, null));
                case "system" -> {
                    // SystemMessages in state["messages"] are platform
                    // directives the worker put there intentionally
                    // (e.g. attached-memory preambles). Render them as
                    // a marker-style system_note so the Console can show
                    // them under "Show details" without mixing them into
                    // the chat flow.
                    turns.add(new ActivityEventResponse(
                            "marker.system_note",
                            timestamp,
                            null,
                            asString(kwargs.get("content")),
                            null, null, null, null,
                            "system_note", null, null, null, null,
                            null, null));
                }
                default -> { /* unknown type — skip */ }
            }
        }
        return turns;
    }

    @SuppressWarnings("unchecked")
    private ActivityEventResponse buildAssistantTurn(
            Map<String, Object> kwargs,
            OffsetDateTime ts,
            Map<String, Long> costByAiMessageId) {
        List<ActivityEventResponse.ToolCall> toolCalls = null;
        Object rawToolCalls = kwargs.get("tool_calls");
        if (rawToolCalls instanceof List<?> rawList && !rawList.isEmpty()) {
            toolCalls = new ArrayList<>(rawList.size());
            for (Object entry : rawList) {
                if (!(entry instanceof Map<?, ?> callMap)) continue;
                Map<String, Object> call = (Map<String, Object>) callMap;
                toolCalls.add(new ActivityEventResponse.ToolCall(
                        asString(call.get("id")),
                        asString(call.get("name")),
                        call.get("args")));
            }
            if (toolCalls.isEmpty()) {
                toolCalls = null;
            }
        }

        Map<String, Integer> usage = extractUsage(kwargs.get("usage_metadata"));
        Long cost = null;
        String messageId = asString(kwargs.get("id"));
        if (messageId != null && costByAiMessageId.containsKey(messageId)) {
            cost = costByAiMessageId.get(messageId);
        }

        return new ActivityEventResponse(
                "turn.assistant",
                ts,
                "assistant",
                asString(kwargs.get("content")),
                null, null,
                toolCalls,
                null,
                null, null, null, null, null,
                usage,
                cost);
    }

    /**
     * Reads the three token counters off a LangChain {@code usage_metadata}
     * dict. Returns {@code null} when no usable data is present so
     * {@link com.fasterxml.jackson.annotation.JsonInclude} keeps the
     * response compact for legacy turns (pre-Track-7 AIMessages without
     * usage).
     */
    @SuppressWarnings("unchecked")
    private Map<String, Integer> extractUsage(Object raw) {
        if (!(raw instanceof Map<?, ?> map)) {
            return null;
        }
        Map<String, Object> usage = (Map<String, Object>) map;
        Integer in = readInt(usage.get("input_tokens"));
        Integer out = readInt(usage.get("output_tokens"));
        Integer total = readInt(usage.get("total_tokens"));
        if (in == null && out == null && total == null) {
            return null;
        }
        Map<String, Integer> result = new HashMap<>();
        if (in != null) result.put("input_tokens", in);
        if (out != null) result.put("output_tokens", out);
        if (total != null) result.put("total_tokens", total);
        return result;
    }

    private Integer readInt(Object value) {
        if (value instanceof Number n) {
            return n.intValue();
        }
        return null;
    }

    private OffsetDateTime readEmittedAt(Map<String, Object> kwargs) {
        Object additional = kwargs.get("additional_kwargs");
        if (!(additional instanceof Map<?, ?> additionalMap)) {
            return null;
        }
        String raw = asString(additionalMap.get("emitted_at"));
        if (raw == null || raw.isBlank()) {
            return null;
        }
        try {
            return OffsetDateTime.parse(raw);
        } catch (DateTimeParseException e) {
            log.debug("Unparseable emitted_at: {}", raw);
            return null;
        }
    }

    @SuppressWarnings("unchecked")
    private Map<String, Object> parsePayload(Object payload) {
        if (payload == null) {
            return null;
        }
        if (payload instanceof Map<?, ?> map) {
            return (Map<String, Object>) map;
        }
        String json;
        if (payload instanceof org.postgresql.util.PGobject pg) {
            json = pg.getValue();
        } else {
            json = payload.toString();
        }
        if (json == null || json.isBlank()) {
            return null;
        }
        try {
            return objectMapper.readValue(json, Map.class);
        } catch (Exception e) {
            log.warn("Failed to parse checkpoint_payload JSON: {}", e.getMessage());
            return null;
        }
    }

    // ---------------------------------------------------------------------
    // Marker mapping from task_events
    // ---------------------------------------------------------------------

    private ActivityEventResponse mapMarker(TaskEventResponse event) {
        String type = event.eventType();
        if (type == null) {
            return null;
        }
        String kind = switch (type) {
            case "task_compaction_fired" -> "marker.compaction_fired";
            case "memory_flush" -> "marker.memory_flush";
            case "offload_emitted" -> "marker.offload_emitted";
            case "system_note" -> "marker.system_note";
            case "task_paused" -> "marker.hitl.paused";
            case "task_resumed" -> "marker.hitl.resumed";
            case "task_approval_requested" -> "marker.hitl.approval_requested";
            case "task_approved" -> "marker.hitl.approved";
            case "task_rejected" -> "marker.hitl.rejected";
            case "task_input_requested" -> "marker.hitl.input_requested";
            case "task_input_received" -> "marker.hitl.input_received";
            // Lifecycle — coarse-grained bucket so the Console can hide
            // these behind a single "Show details" toggle.
            case "task_submitted", "task_claimed", "task_retry_scheduled",
                 "task_reclaimed_after_lease_expiry", "task_dead_lettered",
                 "task_redriven", "task_completed", "task_cancelled",
                 "task_follow_up" -> "marker.lifecycle";
            default -> null;
        };
        if (kind == null) {
            return null;
        }
        String summaryText = null;
        if ("marker.compaction_fired".equals(kind) && event.details() instanceof Map<?, ?> details) {
            Object st = details.get("summary_text");
            if (st != null) {
                summaryText = st.toString();
            }
        }
        return new ActivityEventResponse(
                kind,
                event.createdAt(),
                null, null, null, null, null, null,
                type,
                event.statusBefore(),
                event.statusAfter(),
                summaryText,
                event.details(),
                null,
                null);
    }

    private static String asString(Object value) {
        if (value == null) return null;
        return value instanceof String s ? s : value.toString();
    }
}
