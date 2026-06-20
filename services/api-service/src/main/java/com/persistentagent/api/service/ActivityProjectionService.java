package com.persistentagent.api.service;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.persistentagent.api.config.ValidationConstants;
import com.persistentagent.api.exception.TaskNotFoundException;
import com.persistentagent.api.model.response.ActivityEventResponse;
import com.persistentagent.api.model.response.TaskEventResponse;
import com.persistentagent.api.repository.TaskEventRepository;
import com.persistentagent.api.repository.TaskRepository;
import com.persistentagent.api.util.DateTimeUtil;
import com.persistentagent.api.util.JsonParseUtil;
import com.persistentagent.api.util.MessageContentExtractor;
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
import java.util.LinkedHashMap;
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
            "marker.hitl.input_received",
            // Issue #102 follow-up — surface successful memory commits as a
            // user-meaningful timeline event so customers see when persistent
            // memory was actually written for the task.
            "marker.memory_written",
            // S9 sub-agent fan-out observability — user-meaningful research progress:
            //   marker.subagent.finding    — a sub-agent emitted a structured finding
            //   marker.subagent.failed     — a sub-agent failed (customer sees failure)
            //   marker.subagent.completed  — a sub-agent finished successfully (terminal;
            //                                the ONLY signal for a zero-finding success —
            //                                without it on the coarse view the Console badge
            //                                is stranded on "running")
            //   marker.supervisor.iteration — supervisor closed a round (progress / stop)
            // Excluded: marker.subagent.started — lifecycle telemetry, detail-only
            "marker.subagent.finding",
            "marker.subagent.failed",
            "marker.subagent.completed",
            "marker.supervisor.iteration"
    );

    /**
     * Dedup key for sub-agent fan-out markers (at-least-once contract).
     *
     * <p>The worker emits these events at-least-once — a crashed-and-resumed
     * inner step re-emits the same event on recovery, producing duplicate rows
     * in {@code task_events}. The dedup key is {@code (event_type, iteration,
     * subtask)} — the same triple the worker puts in the {@code details} JSONB.
     *
     * <p>Resolution policy per kind:
     * <ul>
     *   <li>{@code subagent_started} — first-wins (first row is authoritative)</li>
     *   <li>{@code subagent_finding}, {@code subagent_failed},
     *       {@code supervisor_iteration} — last-wins (final row has the
     *       most-recent result/reason)</li>
     * </ul>
     */
    /**
     * Dedup key for the at-least-once sub-agent markers. {@code findingId}
     * discriminates {@code subagent_finding} rows: one sub-agent emits MANY
     * distinct findings under the same (iteration, subtask), and only a true
     * crash-resume re-emit (same finding_id) may collapse — without it a
     * 16-finding sub-agent projected as a single last-wins row. Null for the
     * other marker types.
     */
    private record SubagentDedupKey(
            String eventType, Integer iteration, String subtask, String findingId) {}

    /** Event types for which duplicates use last-wins dedup (most recent wins). */
    private static final Set<String> SUBAGENT_LAST_WINS = Set.of(
            "subagent_finding", "subagent_failed", "supervisor_iteration"
    );

    /** Event types subject to at-least-once dedup (includes first-wins types too). */
    private static final Set<String> SUBAGENT_DEDUP_TYPES = Set.of(
            "subagent_started", "subagent_finding", "subagent_failed", "supervisor_iteration",
            // subagent_completed is first-wins (NOT in SUBAGENT_LAST_WINS): its
            // {iteration, subtask} payload is fixed, so every at-least-once duplicate
            // row is byte-identical and first-wins keeps the EARLIEST created_at — the
            // true completion timestamp. Pairs with subagent_started (also first-wins),
            // its lifecycle-bracket sibling, rather than the last-wins result/reason
            // markers (finding/failed/iteration) whose payload changes across re-emits.
            "subagent_completed"
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

        // Walk every checkpoint up-front to build (a) the per-AI-message cost
        // attribution and (b) the real created_at of every message (the
        // checkpoint where it first appeared). The second map is what keeps
        // turns sorted correctly relative to task_events markers — without it
        // every turn inherits the *final* checkpoint's created_at and all
        // lifecycle markers end up stacked at the top of the stream.
        TurnAttribution attribution = walkCheckpoints(taskId, tenantId);

        List<ActivityEventResponse> events = new ArrayList<>();

        OffsetDateTime checkpointCreatedAt = null;
        if (checkpoint.isPresent()) {
            Map<String, Object> row = checkpoint.get();
            Object createdAt = row.get("created_at");
            if (createdAt instanceof Timestamp ts) {
                checkpointCreatedAt = DateTimeUtil.toOffsetDateTime(ts);
            }
            Object payload = row.get("checkpoint_payload");
            events.addAll(extractTurns(payload, checkpointCreatedAt, attribution));

            // Supervisor topology: the Writer's deliverable lives in the
            // ``report`` channel, not ``messages`` (which carries only the
            // task input there) — surface it as the terminal assistant turn
            // so the Activity stream ends with the agent's actual output.
            ActivityEventResponse reportTurn = buildReportTurn(payload, checkpointCreatedAt);
            if (reportTurn != null) {
                events.add(reportTurn);
            }
        }

        // Sub-agent transcripts (fan-out topologies): each sub-agent's inner
        // ReAct turns are checkpointed under its own ``subagent:*`` namespace.
        // Project them as turn.* events tagged with the sub-agent's stable
        // ``subtask`` id so the Console can nest them under the S9/S10 marker
        // tree instead of pointing customers at Langfuse.
        events.addAll(extractSubagentTurns(
                taskId, tenantId, startedPreviewBySubtask(markerRows)));

        // At-least-once dedup for sub-agent fan-out markers (S9).
        //
        // The worker emits subagent_* / supervisor_iteration at-least-once:
        // a crashed-and-resumed inner step re-emits its events on recovery,
        // producing duplicate rows. We dedup by (event_type, iteration, subtask)
        // using insertion-ordered maps so the final list preserves first-seen
        // ordering. markerRows arrives in created_at ASC order from the DB.
        //
        // Resolution policy: first-wins for subagent_started (authoritative
        // dispatch info) and subagent_completed (fixed payload — earliest row is
        // the true completion time); last-wins for subagent_finding /
        // subagent_failed / supervisor_iteration (most-recent result/reason wins).
        //
        // Non-sub-agent markers are not subject to this dedup and flow through
        // unchanged (they already have unique event ids from the DB).
        Map<SubagentDedupKey, ActivityEventResponse> dedupMap = new LinkedHashMap<>();
        List<ActivityEventResponse> nonDedupMarkers = new ArrayList<>();

        for (TaskEventResponse marker : markerRows) {
            ActivityEventResponse mapped = mapMarker(marker);
            if (mapped == null) {
                continue;
            }
            if (!includeDetails && !USER_VISIBLE_MARKERS.contains(mapped.kind())) {
                continue;
            }
            if (SUBAGENT_DEDUP_TYPES.contains(marker.eventType()) && mapped.iteration() != null) {
                // finding rows carry their finding_id in the key — distinct
                // findings from one sub-agent must NOT collapse (see the
                // SubagentDedupKey javadoc).
                String findingId = null;
                if ("subagent_finding".equals(marker.eventType())
                        && marker.details() instanceof Map<?, ?> detailsMap) {
                    Object fid = detailsMap.get("finding_id");
                    findingId = fid != null ? fid.toString() : null;
                }
                SubagentDedupKey key = new SubagentDedupKey(
                        marker.eventType(), mapped.iteration(), mapped.subtask(), findingId);
                if (SUBAGENT_LAST_WINS.contains(marker.eventType())) {
                    // last-wins: overwrite any prior entry
                    dedupMap.put(key, mapped);
                } else {
                    // first-wins: only insert if key not yet seen
                    dedupMap.putIfAbsent(key, mapped);
                }
            } else {
                nonDedupMarkers.add(mapped);
            }
        }

        events.addAll(nonDedupMarkers);
        events.addAll(dedupMap.values());

        // Stable sort by timestamp. Turn timestamps fall back to the
        // containing checkpoint's created_at when emitted_at is absent —
        // this produces a coarse but monotone ordering for historical
        // tasks.
        events.sort(Comparator.comparing(ActivityEventResponse::timestamp,
                Comparator.nullsLast(Comparator.naturalOrder())));

        boolean truncated = events.size() > MAX_EVENTS;
        if (truncated) {
            events = events.subList(0, MAX_EVENTS);
        }

        return new ActivityEventResponse.Page(events, null, truncated ? Boolean.TRUE : null);
    }

    // ---------------------------------------------------------------------
    // Per-turn attribution — walks all checkpoints in order and records, for
    // every message id it sees, (a) the created_at of the checkpoint where
    // it first appeared (real timestamp, vs the final-checkpoint fallback)
    // and (b) on AI messages only, the sum of cost_microdollars for the
    // checkpoint that minted it. Parsing every payload is not free but the
    // checkpoint count per task stays O(100s) in practice.
    // ---------------------------------------------------------------------

    /** Attribution map keyed on message id. Never contains null values. */
    private record TurnAttribution(
            Map<String, OffsetDateTime> firstSeenAt,
            Map<String, Long> costByAiMessageId,
            Map<String, String> workerByMessageId) {
        static TurnAttribution empty() {
            return new TurnAttribution(
                    Collections.emptyMap(),
                    Collections.emptyMap(),
                    Collections.emptyMap());
        }
    }

    private TurnAttribution walkCheckpoints(UUID taskId, String tenantId) {
        var all = taskRepository.getCheckpoints(taskId, tenantId).orElse(Collections.emptyList());
        return walkRows(all, "messages");
    }

    /**
     * Walks the given checkpoint rows in order, recording per-message first-seen
     * timestamps, worker ids, and (AI messages only) minting-checkpoint cost.
     * {@code channel} names the message-list channel to read — {@code messages}
     * for the root namespace, {@code sub_messages} for sub-agent namespaces.
     */
    @SuppressWarnings("unchecked")
    private TurnAttribution walkRows(List<Map<String, Object>> all, String channel) {
        if (all.isEmpty()) {
            return TurnAttribution.empty();
        }
        Map<String, OffsetDateTime> firstSeenAt = new HashMap<>();
        Map<String, Long> costByAiMessageId = new HashMap<>();
        Map<String, String> workerByMessageId = new HashMap<>();
        Set<String> seen = new HashSet<>();
        for (Map<String, Object> row : all) {
            Object costObj = row.get("cost_microdollars");
            long cost = 0;
            if (costObj instanceof Number n) {
                cost = n.longValue();
            }
            OffsetDateTime rowCreatedAt = null;
            Object createdAtObj = row.get("created_at");
            if (createdAtObj instanceof Timestamp ts) {
                rowCreatedAt = DateTimeUtil.toOffsetDateTime(ts);
            }
            String rowWorkerId = asString(row.get("worker_id"));
            Object payload = row.get("checkpoint_payload");
            Map<String, Object> parsed = JsonParseUtil.parseJsonMap(objectMapper, payload);
            if (parsed == null) {
                continue;
            }
            Object channelValues = parsed.get("channel_values");
            if (!(channelValues instanceof Map<?, ?> channelMap)) {
                continue;
            }
            Object messages = ((Map<String, Object>) channelMap).get(channel);
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
                if (!seen.contains(id)) {
                    seen.add(id);
                    if (rowCreatedAt != null) {
                        firstSeenAt.putIfAbsent(id, rowCreatedAt);
                    }
                    if (rowWorkerId != null && !rowWorkerId.isBlank()) {
                        workerByMessageId.putIfAbsent(id, rowWorkerId);
                    }
                    if ("ai".equals(type) && firstNewAiId == null) {
                        firstNewAiId = id;
                    }
                }
            }
            if (cost > 0 && firstNewAiId != null) {
                costByAiMessageId.merge(firstNewAiId, cost, Long::sum);
            }
        }
        return new TurnAttribution(firstSeenAt, costByAiMessageId, workerByMessageId);
    }

    // ---------------------------------------------------------------------
    // Turn extraction from checkpoint_payload.channel_values.messages
    // ---------------------------------------------------------------------

    private List<ActivityEventResponse> extractTurns(
            Object payload,
            OffsetDateTime fallbackTs,
            TurnAttribution attribution) {
        List<?> messageList = readMessageChannel(payload, "messages");
        if (messageList == null) {
            return new ArrayList<>();
        }
        return turnsFromMessageList(messageList, null, fallbackTs, attribution);
    }

    /** Parses a checkpoint payload and returns the named message-list channel, or null. */
    @SuppressWarnings("unchecked")
    private List<?> readMessageChannel(Object payload, String channel) {
        Map<String, Object> parsed = JsonParseUtil.parseJsonMap(objectMapper, payload);
        if (parsed == null) {
            return null;
        }
        Object channelValues = parsed.get("channel_values");
        if (!(channelValues instanceof Map<?, ?> channelMap)) {
            return null;
        }
        Object messages = ((Map<String, Object>) channelMap).get(channel);
        return messages instanceof List<?> messageList ? messageList : null;
    }

    /**
     * Maps a langchain-serialized message list to turn events. {@code subtask}
     * is null for the root conversation; for sub-agent transcripts it carries
     * the sub-agent's stable id so the Console can nest the turns under the
     * fan-out marker tree.
     */
    @SuppressWarnings("unchecked")
    private List<ActivityEventResponse> turnsFromMessageList(
            List<?> messageList,
            String subtask,
            OffsetDateTime fallbackTs,
            TurnAttribution attribution) {
        List<ActivityEventResponse> turns = new ArrayList<>();
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

            // Timestamp precedence: the checkpoint where the message first
            // appeared > `additional_kwargs.emitted_at` (only set on newer
            // messages) > the final checkpoint's created_at (coarse fallback
            // that preserves ordering within the message list for historical
            // tasks lacking both other signals).
            String messageId = asString(kwargs.get("id"));
            OffsetDateTime timestamp = null;
            if (messageId != null) {
                timestamp = attribution.firstSeenAt().get(messageId);
            }
            if (timestamp == null) {
                timestamp = readEmittedAt(kwargs);
            }
            if (timestamp == null) {
                timestamp = fallbackTs;
            }

            String workerId = messageId != null
                    ? attribution.workerByMessageId().get(messageId)
                    : null;

            switch (type) {
                case "human" -> turns.add(new ActivityEventResponse(
                        "turn.user",
                        timestamp,
                        "user",
                        MessageContentExtractor.extractText(kwargs.get("content")),
                        null, null, null, null,
                        null, null, null, null, null,
                        null, null,
                        workerId, null,
                        null, subtask));
                case "ai" -> turns.add(buildAssistantTurn(
                        kwargs, timestamp, attribution.costByAiMessageId(), workerId, subtask));
                case "tool" -> turns.add(new ActivityEventResponse(
                        "turn.tool",
                        timestamp,
                        "tool",
                        MessageContentExtractor.extractText(kwargs.get("content")),
                        asString(kwargs.get("name")),
                        asString(kwargs.get("tool_call_id")),
                        null,
                        "error".equalsIgnoreCase(asString(kwargs.get("status"))),
                        null, null, null, null, null,
                        null, null,
                        workerId,
                        readOrigBytes(kwargs),
                        null, subtask));
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
                            MessageContentExtractor.extractText(kwargs.get("content")),
                            null, null, null, null,
                            "system_note", null, null, null, null,
                            null, null,
                            null, null,
                            null, subtask));
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
            Map<String, Long> costByAiMessageId,
            String workerId,
            String subtask) {
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
                MessageContentExtractor.extractText(kwargs.get("content")),
                null, null,
                toolCalls,
                null,
                null, null, null, null, null,
                usage,
                cost,
                workerId,
                null,
                null, subtask);
    }

    // ---------------------------------------------------------------------
    // Supervisor topology projections — sub-agent transcripts + final report
    // ---------------------------------------------------------------------

    /**
     * Builds {@code subtask → prompt_preview} from the {@code subagent_started}
     * markers — the historic-correlation hint for sub-checkpoints written
     * before the worker seeded the {@code subtask} channel. The preview is a
     * verbatim prefix (≤200 chars) of the raw sub-task prompt, which the
     * transcript's first HumanMessage embeds.
     */
    @SuppressWarnings("unchecked")
    private Map<String, String> startedPreviewBySubtask(List<TaskEventResponse> markerRows) {
        Map<String, String> previewBySubtask = new LinkedHashMap<>();
        for (TaskEventResponse marker : markerRows) {
            if (!"subagent_started".equals(marker.eventType())) {
                continue;
            }
            if (!(marker.details() instanceof Map<?, ?> detailsMap)) {
                continue;
            }
            Map<String, Object> details = (Map<String, Object>) detailsMap;
            Object subtask = details.get("subtask");
            Object preview = details.get("prompt_preview");
            if (subtask != null && preview instanceof String p && !p.isBlank()) {
                previewBySubtask.putIfAbsent(subtask.toString(), p);
            }
        }
        return previewBySubtask;
    }

    /**
     * Resolves a historic (channel-less) transcript to its subtask id by
     * matching the {@code subagent_started} prompt previews against the
     * transcript's first HumanMessage. Returns {@code null} unless exactly one
     * subtask's preview matches — ambiguity falls back to the namespace label
     * rather than guessing.
     */
    @SuppressWarnings("unchecked")
    private String correlateByPromptPreview(
            List<?> messageList, Map<String, String> previewBySubtask) {
        if (previewBySubtask.isEmpty()) {
            return null;
        }
        String firstHuman = null;
        for (Object rawMessage : messageList) {
            if (!(rawMessage instanceof Map<?, ?> wrapper)) {
                continue;
            }
            Object rawKwargs = ((Map<String, Object>) wrapper).get("kwargs");
            if (!(rawKwargs instanceof Map<?, ?> kwargsMap)) {
                continue;
            }
            Map<String, Object> kwargs = (Map<String, Object>) kwargsMap;
            if ("human".equals(asString(kwargs.get("type")))) {
                firstHuman = MessageContentExtractor.extractText(kwargs.get("content"));
                break;
            }
        }
        if (firstHuman == null || firstHuman.isBlank()) {
            return null;
        }
        String matched = null;
        for (Map.Entry<String, String> entry : previewBySubtask.entrySet()) {
            if (firstHuman.contains(entry.getValue())) {
                if (matched != null) {
                    return null; // ambiguous — two subtasks share the preview
                }
                matched = entry.getKey();
            }
        }
        return matched;
    }

    /**
     * Projects every sub-agent's checkpointed transcript ({@code sub_messages}
     * under its {@code subagent:*} namespace) into turn events tagged with the
     * sub-agent's stable {@code subtask} id. The id is seeded into the
     * sub-agent state by the worker; historic checkpoints written before that
     * seed are correlated via the {@code subagent_started} prompt previews,
     * falling back to a short namespace-derived label so the transcript still
     * renders (just without marker-tree nesting).
     */
    @SuppressWarnings("unchecked")
    private List<ActivityEventResponse> extractSubagentTurns(
            UUID taskId, String tenantId, Map<String, String> previewBySubtask) {
        List<Map<String, Object>> rows = taskRepository.getSubagentCheckpoints(taskId, tenantId);
        if (rows == null || rows.isEmpty()) {
            return List.of();
        }
        Map<String, List<Map<String, Object>>> byNamespace = new LinkedHashMap<>();
        for (Map<String, Object> row : rows) {
            String ns = asString(row.get("checkpoint_ns"));
            if (ns == null || ns.isBlank()) {
                continue;
            }
            byNamespace.computeIfAbsent(ns, k -> new ArrayList<>()).add(row);
        }

        List<ActivityEventResponse> turns = new ArrayList<>();
        for (Map.Entry<String, List<Map<String, Object>>> entry : byNamespace.entrySet()) {
            List<Map<String, Object>> nsRows = entry.getValue();
            TurnAttribution attribution = walkRows(nsRows, "sub_messages");
            Map<String, Object> last = nsRows.get(nsRows.size() - 1);

            OffsetDateTime fallbackTs = null;
            if (last.get("created_at") instanceof Timestamp ts) {
                fallbackTs = DateTimeUtil.toOffsetDateTime(ts);
            }
            Map<String, Object> parsed = JsonParseUtil.parseJsonMap(objectMapper, last.get("checkpoint_payload"));
            if (parsed == null) {
                continue;
            }
            Object channelValues = parsed.get("channel_values");
            if (!(channelValues instanceof Map<?, ?> channelMap)) {
                continue;
            }
            Map<String, Object> channels = (Map<String, Object>) channelMap;
            Object messages = channels.get("sub_messages");
            if (!(messages instanceof List<?> messageList)) {
                continue;
            }
            String subtask = asString(channels.get("subtask"));
            if (subtask == null || subtask.isBlank()) {
                subtask = correlateByPromptPreview(messageList, previewBySubtask);
            }
            if (subtask == null || subtask.isBlank()) {
                subtask = shortNamespaceLabel(entry.getKey());
            }
            turns.addAll(turnsFromMessageList(messageList, subtask, fallbackTs, attribution));
        }
        return turns;
    }

    /** {@code subagent:467974c0-aa97-...} → {@code sub-467974c0} (historic fallback id). */
    private static String shortNamespaceLabel(String namespace) {
        String suffix = namespace.substring(namespace.indexOf(':') + 1);
        int dash = suffix.indexOf('-');
        return "sub-" + (dash > 0 ? suffix.substring(0, dash) : suffix);
    }

    /**
     * Builds the terminal assistant turn from the Supervisor topology's
     * {@code report} channel, or returns {@code null} for ReAct tasks (no
     * such channel) and unfinished supervisor runs (blank report). Timestamp
     * is the final checkpoint's {@code created_at}, which sorts the report
     * after every conversation turn.
     */
    @SuppressWarnings("unchecked")
    private ActivityEventResponse buildReportTurn(Object payload, OffsetDateTime ts) {
        Map<String, Object> parsed = JsonParseUtil.parseJsonMap(objectMapper, payload);
        if (parsed == null) {
            return null;
        }
        Object channelValues = parsed.get("channel_values");
        if (!(channelValues instanceof Map<?, ?> channelMap)) {
            return null;
        }
        Object report = ((Map<String, Object>) channelMap).get("report");
        if (!(report instanceof String text) || text.isBlank()) {
            return null;
        }
        return new ActivityEventResponse(
                "turn.assistant",
                ts,
                "assistant",
                text,
                null, null, null, null,
                null, null, null, null, null,
                null, null,
                null, null,
                null, null);
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

    // ---------------------------------------------------------------------
    // Marker mapping from task_events
    // ---------------------------------------------------------------------

    @SuppressWarnings("unchecked")
    private ActivityEventResponse mapMarker(TaskEventResponse event) {
        String type = event.eventType();
        if (type == null) {
            return null;
        }
        String kind = switch (type) {
            case "task_compaction_fired" -> "marker.compaction_fired";
            case "memory_flush" -> "marker.memory_flush";
            case "memory_written" -> "marker.memory_written";
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
            // S9 sub-agent fan-out observability markers.
            // These carry iteration (round, 1-based; supervisor_iteration cap/no-op
            // events may carry 0) and subtask (stable logical
            // id) from the details JSONB so the Console can group by round then
            // sub-agent. Turn-by-turn sub-agent transcripts are projected
            // separately from the subagent:* checkpoint namespaces (see
            // extractSubagentTurns) and carry the same subtask key.
            case "subagent_started" -> "marker.subagent.started";
            case "subagent_finding" -> "marker.subagent.finding";
            case "subagent_failed" -> "marker.subagent.failed";
            case "subagent_completed" -> "marker.subagent.completed";
            case "supervisor_iteration" -> "marker.supervisor.iteration";
            // Forward-compat: unknown event_type written by a newer worker
            // against an older API is silently dropped (not errored).
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

        // Lift iteration / subtask out of details for the sub-agent marker kinds.
        // Guard for missing keys and non-Number iteration, mirroring the summaryText
        // guard pattern above. subtask is present on subagent_* but not on
        // supervisor_iteration (which has no per-sub-agent scope).
        Integer iteration = null;
        String subtask = null;
        if (SUBAGENT_DEDUP_TYPES.contains(type) && event.details() instanceof Map<?, ?> detailsMap) {
            Map<String, Object> details = (Map<String, Object>) detailsMap;
            Object iterObj = details.get("iteration");
            if (iterObj instanceof Number n) {
                iteration = n.intValue();
            }
            Object subtaskObj = details.get("subtask");
            if (subtaskObj != null) {
                subtask = subtaskObj.toString();
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
                null,
                null,
                null,
                iteration,
                subtask);
    }

    /**
     * Walks {@code kwargs.additional_kwargs.orig_bytes} and returns the
     * pre-truncation byte count the worker stashed when it truncated a
     * large tool output. Returns {@code null} when either the
     * {@code additional_kwargs} map is absent (legacy ToolMessages) or the
     * {@code orig_bytes} key is missing.
     */
    @SuppressWarnings("unchecked")
    private Long readOrigBytes(Map<String, Object> kwargs) {
        Object additional = kwargs.get("additional_kwargs");
        if (!(additional instanceof Map<?, ?> additionalMap)) {
            return null;
        }
        Object raw = ((Map<String, Object>) additionalMap).get("orig_bytes");
        if (raw instanceof Number n) {
            return n.longValue();
        }
        return null;
    }

    private static String asString(Object value) {
        if (value == null) return null;
        return value instanceof String s ? s : value.toString();
    }
}
