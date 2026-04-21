package com.persistentagent.api.service;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.datatype.jsr310.JavaTimeModule;
import com.persistentagent.api.config.ValidationConstants;
import com.persistentagent.api.exception.TaskNotFoundException;
import com.persistentagent.api.model.response.ActivityEventResponse;
import com.persistentagent.api.model.response.TaskEventResponse;
import com.persistentagent.api.repository.TaskEventRepository;
import com.persistentagent.api.repository.TaskRepository;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.Arguments;
import org.junit.jupiter.params.provider.MethodSource;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;

import java.sql.Timestamp;
import java.time.Instant;
import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.UUID;
import java.util.stream.Stream;

import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.ArgumentMatchers.anyInt;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.Mockito.when;

/**
 * Phase 2 Track 7 Follow-up Task 8 (B) — projection merge + filter behavior.
 *
 * <p>Pure Mockito — no DB. The checkpoint payload here mirrors the
 * {@code langchain_dumps} shape the worker writes (see §Task A): each
 * message is {@code {lc, type: "constructor", id: [...], kwargs: {...}}}.
 */
@ExtendWith(MockitoExtension.class)
class ActivityProjectionServiceTest {

    @Mock private TaskRepository taskRepository;
    @Mock private TaskEventRepository taskEventRepository;

    private ActivityProjectionService service;
    private ObjectMapper objectMapper;

    @BeforeEach
    void setUp() {
        objectMapper = new ObjectMapper();
        objectMapper.registerModule(new JavaTimeModule());
        service = new ActivityProjectionService(taskRepository, taskEventRepository, objectMapper);
    }

    // --- Task existence / tenant isolation ---

    @Test
    void getActivity_noCheckpointAndNoMarkers_404IfTaskMissing() {
        UUID taskId = UUID.randomUUID();
        String tenantId = ValidationConstants.DEFAULT_TENANT_ID;
        when(taskRepository.getLatestRootCheckpoint(taskId, tenantId)).thenReturn(Optional.empty());
        when(taskEventRepository.listEvents(eq(taskId), eq(tenantId), anyInt())).thenReturn(List.of());
        when(taskRepository.findByIdAndTenant(taskId, tenantId)).thenReturn(Optional.empty());

        assertThrows(TaskNotFoundException.class, () -> service.getActivity(taskId, false));
    }

    @Test
    void getActivity_noCheckpointButTaskExists_returnsEmptyStream() {
        UUID taskId = UUID.randomUUID();
        String tenantId = ValidationConstants.DEFAULT_TENANT_ID;
        when(taskRepository.getLatestRootCheckpoint(taskId, tenantId)).thenReturn(Optional.empty());
        when(taskEventRepository.listEvents(eq(taskId), eq(tenantId), anyInt())).thenReturn(List.of());
        when(taskRepository.findByIdAndTenant(taskId, tenantId))
                .thenReturn(Optional.of(Map.of("task_id", taskId)));

        ActivityEventResponse.Page page = service.getActivity(taskId, false);
        assertTrue(page.events().isEmpty());
        assertNull(page.nextCursor());
    }

    // --- Turn extraction ---

    @Test
    void getActivity_extractsHumanAiAndToolTurns() throws Exception {
        UUID taskId = UUID.randomUUID();
        String tenantId = ValidationConstants.DEFAULT_TENANT_ID;

        String payload = """
                {
                  "channel_values": {
                    "messages": [
                      {"lc":1,"type":"constructor","id":["x","y","z","HumanMessage"],
                       "kwargs":{"type":"human","content":"hello",
                                 "additional_kwargs":{"emitted_at":"2026-04-20T00:00:00+00:00"}}},
                      {"lc":1,"type":"constructor","id":["x","y","z","AIMessage"],
                       "kwargs":{"type":"ai","content":"sure",
                                 "additional_kwargs":{"emitted_at":"2026-04-20T00:00:01+00:00"},
                                 "tool_calls":[{"id":"call_1","name":"ls","args":{"path":"/tmp"}}]}},
                      {"lc":1,"type":"constructor","id":["x","y","z","ToolMessage"],
                       "kwargs":{"type":"tool","content":"file1\\nfile2","name":"ls",
                                 "tool_call_id":"call_1","status":"success",
                                 "additional_kwargs":{"emitted_at":"2026-04-20T00:00:02+00:00"}}}
                    ]
                  }
                }
                """;

        Timestamp created = Timestamp.from(Instant.parse("2026-04-20T00:00:00Z"));
        when(taskRepository.getLatestRootCheckpoint(taskId, tenantId)).thenReturn(Optional.of(Map.of(
                "checkpoint_id", "ckpt_1",
                "checkpoint_payload", payload,
                "created_at", created)));
        when(taskEventRepository.listEvents(eq(taskId), eq(tenantId), anyInt())).thenReturn(List.of());

        ActivityEventResponse.Page page = service.getActivity(taskId, false);
        List<ActivityEventResponse> events = page.events();

        assertEquals(3, events.size());
        assertEquals("turn.user", events.get(0).kind());
        assertEquals("hello", events.get(0).content());
        assertEquals(OffsetDateTime.of(2026, 4, 20, 0, 0, 0, 0, ZoneOffset.UTC), events.get(0).timestamp());

        assertEquals("turn.assistant", events.get(1).kind());
        assertEquals("sure", events.get(1).content());
        assertNotNull(events.get(1).toolCalls());
        assertEquals(1, events.get(1).toolCalls().size());
        assertEquals("ls", events.get(1).toolCalls().get(0).name());

        assertEquals("turn.tool", events.get(2).kind());
        assertEquals("ls", events.get(2).toolName());
        assertEquals("call_1", events.get(2).toolCallId());
        assertEquals("file1\nfile2", events.get(2).content());
        assertFalse(events.get(2).isError());
    }

    @Test
    void getActivity_fallsBackToCheckpointCreatedAt_whenEmittedAtMissing() {
        UUID taskId = UUID.randomUUID();
        String tenantId = ValidationConstants.DEFAULT_TENANT_ID;

        String payload = """
                {"channel_values":{"messages":[
                   {"lc":1,"type":"constructor","id":["_","HumanMessage"],
                    "kwargs":{"type":"human","content":"legacy"}}
                 ]}}
                """;

        Timestamp created = Timestamp.from(Instant.parse("2024-01-01T00:00:00Z"));
        when(taskRepository.getLatestRootCheckpoint(taskId, tenantId)).thenReturn(Optional.of(Map.of(
                "checkpoint_id", "ckpt_1",
                "checkpoint_payload", payload,
                "created_at", created)));
        when(taskEventRepository.listEvents(eq(taskId), eq(tenantId), anyInt())).thenReturn(List.of());

        List<ActivityEventResponse> events = service.getActivity(taskId, false).events();
        assertEquals(1, events.size());
        assertEquals(OffsetDateTime.of(2024, 1, 1, 0, 0, 0, 0, ZoneOffset.UTC), events.get(0).timestamp());
    }

    // --- Marker mapping + include_details filter ---

    @Test
    void getActivity_mapsTaskEventsToMarkerKinds() {
        UUID taskId = UUID.randomUUID();
        String tenantId = ValidationConstants.DEFAULT_TENANT_ID;

        when(taskRepository.getLatestRootCheckpoint(taskId, tenantId)).thenReturn(Optional.empty());

        TaskEventResponse compaction = new TaskEventResponse(
                UUID.randomUUID(), taskId, "agent-1", "task_compaction_fired",
                null, null, "worker-1", null, null,
                Map.of("summary_text", "prior turns summarized", "tokens_in", 1000),
                OffsetDateTime.parse("2026-04-20T00:00:05+00:00"));
        TaskEventResponse memoryFlush = new TaskEventResponse(
                UUID.randomUUID(), taskId, "agent-1", "memory_flush",
                null, null, "worker-1", null, null,
                Map.of("fired_at_step", 3),
                OffsetDateTime.parse("2026-04-20T00:00:06+00:00"));
        TaskEventResponse offload = new TaskEventResponse(
                UUID.randomUUID(), taskId, "agent-1", "offload_emitted",
                null, null, "worker-1", null, null,
                Map.of("count", 2, "total_bytes", 4096),
                OffsetDateTime.parse("2026-04-20T00:00:07+00:00"));
        TaskEventResponse lifecycle = new TaskEventResponse(
                UUID.randomUUID(), taskId, "agent-1", "task_completed",
                "running", "completed", "worker-1", null, null,
                Map.of(),
                OffsetDateTime.parse("2026-04-20T00:00:08+00:00"));
        TaskEventResponse hitlPaused = new TaskEventResponse(
                UUID.randomUUID(), taskId, "agent-1", "task_paused",
                "running", "paused", "worker-1", null, null,
                Map.of("reason", "tool_requires_approval", "tool_name", "delete_file"),
                OffsetDateTime.parse("2026-04-20T00:00:09+00:00"));

        when(taskEventRepository.listEvents(eq(taskId), eq(tenantId), anyInt()))
                .thenReturn(List.of(compaction, memoryFlush, offload, lifecycle, hitlPaused));

        // include_details=true → all markers visible.
        List<ActivityEventResponse> all = service.getActivity(taskId, true).events();
        List<String> kinds = all.stream().map(ActivityEventResponse::kind).toList();
        assertTrue(kinds.contains("marker.compaction_fired"));
        assertTrue(kinds.contains("marker.memory_flush"));
        assertTrue(kinds.contains("marker.offload_emitted"));
        assertTrue(kinds.contains("marker.lifecycle"));
        assertTrue(kinds.contains("marker.hitl.paused"));

        ActivityEventResponse compactionEvent = all.stream()
                .filter(e -> "marker.compaction_fired".equals(e.kind()))
                .findFirst().orElseThrow();
        assertEquals("prior turns summarized", compactionEvent.summaryText());

        // include_details=false → infra markers filtered; user-visible remain.
        List<ActivityEventResponse> userVisible = service.getActivity(taskId, false).events();
        List<String> userKinds = userVisible.stream().map(ActivityEventResponse::kind).toList();
        assertTrue(userKinds.contains("marker.compaction_fired"));
        assertTrue(userKinds.contains("marker.hitl.paused"));
        assertFalse(userKinds.contains("marker.memory_flush"));
        assertFalse(userKinds.contains("marker.offload_emitted"));
        assertFalse(userKinds.contains("marker.lifecycle"));
    }

    @Test
    void getActivity_interleavesTurnsAndMarkersByTimestamp() {
        UUID taskId = UUID.randomUUID();
        String tenantId = ValidationConstants.DEFAULT_TENANT_ID;

        String payload = """
                {"channel_values":{"messages":[
                  {"lc":1,"type":"constructor","id":["_","HumanMessage"],
                   "kwargs":{"type":"human","content":"a",
                             "additional_kwargs":{"emitted_at":"2026-04-20T00:00:00+00:00"}}},
                  {"lc":1,"type":"constructor","id":["_","AIMessage"],
                   "kwargs":{"type":"ai","content":"b",
                             "additional_kwargs":{"emitted_at":"2026-04-20T00:00:10+00:00"}}}
                ]}}
                """;
        Timestamp created = Timestamp.from(Instant.parse("2026-04-20T00:00:00Z"));
        when(taskRepository.getLatestRootCheckpoint(taskId, tenantId)).thenReturn(Optional.of(Map.of(
                "checkpoint_id", "c1",
                "checkpoint_payload", payload,
                "created_at", created)));

        TaskEventResponse middleMarker = new TaskEventResponse(
                UUID.randomUUID(), taskId, "a", "task_compaction_fired",
                null, null, "w", null, null,
                Map.of("summary_text", "s"),
                OffsetDateTime.parse("2026-04-20T00:00:05+00:00"));
        when(taskEventRepository.listEvents(eq(taskId), eq(tenantId), anyInt()))
                .thenReturn(List.of(middleMarker));

        List<ActivityEventResponse> events = service.getActivity(taskId, false).events();
        assertEquals(3, events.size());
        assertEquals("turn.user", events.get(0).kind());
        assertEquals("marker.compaction_fired", events.get(1).kind());
        assertEquals("turn.assistant", events.get(2).kind());
    }

    // --- worker_id attribution ---

    @Test
    void getActivity_surfacesWorkerIdFromCheckpointRow() {
        UUID taskId = UUID.randomUUID();
        String tenantId = ValidationConstants.DEFAULT_TENANT_ID;

        String payload = """
                {"channel_values":{"messages":[
                  {"lc":1,"type":"constructor","id":["x","HumanMessage"],
                   "kwargs":{"type":"human","content":"hi","id":"m_user",
                             "additional_kwargs":{"emitted_at":"2026-04-20T00:00:00+00:00"}}},
                  {"lc":1,"type":"constructor","id":["x","AIMessage"],
                   "kwargs":{"type":"ai","content":"hello","id":"m_ai",
                             "additional_kwargs":{"emitted_at":"2026-04-20T00:00:01+00:00"}}},
                  {"lc":1,"type":"constructor","id":["x","ToolMessage"],
                   "kwargs":{"type":"tool","content":"out","name":"ls",
                             "tool_call_id":"call_1","id":"m_tool",
                             "additional_kwargs":{"emitted_at":"2026-04-20T00:00:02+00:00"}}}
                ]}}
                """;

        Timestamp created = Timestamp.from(Instant.parse("2026-04-20T00:00:00Z"));
        Map<String, Object> row = new java.util.HashMap<>();
        row.put("checkpoint_id", "ckpt_1");
        row.put("worker_id", "worker-abc");
        row.put("cost_microdollars", 0L);
        row.put("checkpoint_payload", payload);
        row.put("created_at", created);

        when(taskRepository.getLatestRootCheckpoint(taskId, tenantId)).thenReturn(Optional.of(row));
        when(taskRepository.getCheckpoints(taskId, tenantId)).thenReturn(Optional.of(List.of(row)));
        when(taskEventRepository.listEvents(eq(taskId), eq(tenantId), anyInt())).thenReturn(List.of());

        List<ActivityEventResponse> events = service.getActivity(taskId, false).events();
        assertEquals(3, events.size());
        assertEquals("worker-abc", events.get(0).workerId());
        assertEquals("worker-abc", events.get(1).workerId());
        assertEquals("worker-abc", events.get(2).workerId());
    }

    @Test
    void getActivity_nullWorkerIdRow_leavesWorkerIdNull() {
        UUID taskId = UUID.randomUUID();
        String tenantId = ValidationConstants.DEFAULT_TENANT_ID;

        String payload = """
                {"channel_values":{"messages":[
                  {"lc":1,"type":"constructor","id":["x","HumanMessage"],
                   "kwargs":{"type":"human","content":"hi","id":"m_user",
                             "additional_kwargs":{"emitted_at":"2026-04-20T00:00:00+00:00"}}}
                ]}}
                """;

        Timestamp created = Timestamp.from(Instant.parse("2026-04-20T00:00:00Z"));
        Map<String, Object> row = new java.util.HashMap<>();
        row.put("checkpoint_id", "ckpt_1");
        row.put("worker_id", null); // nullable TEXT column
        row.put("cost_microdollars", 0L);
        row.put("checkpoint_payload", payload);
        row.put("created_at", created);

        when(taskRepository.getLatestRootCheckpoint(taskId, tenantId)).thenReturn(Optional.of(row));
        when(taskRepository.getCheckpoints(taskId, tenantId)).thenReturn(Optional.of(List.of(row)));
        when(taskEventRepository.listEvents(eq(taskId), eq(tenantId), anyInt())).thenReturn(List.of());

        List<ActivityEventResponse> events = service.getActivity(taskId, false).events();
        assertEquals(1, events.size());
        assertNull(events.get(0).workerId());
    }

    // --- orig_bytes on tool messages ---

    @Test
    void getActivity_toolTurn_surfacesOrigBytesFromAdditionalKwargs() {
        UUID taskId = UUID.randomUUID();
        String tenantId = ValidationConstants.DEFAULT_TENANT_ID;

        String payload = """
                {"channel_values":{"messages":[
                  {"lc":1,"type":"constructor","id":["x","ToolMessage"],
                   "kwargs":{"type":"tool","content":"head...tail","name":"grep",
                             "tool_call_id":"call_x","status":"success",
                             "additional_kwargs":{
                                "emitted_at":"2026-04-20T00:00:00+00:00",
                                "orig_bytes":98765}}}
                ]}}
                """;

        Timestamp created = Timestamp.from(Instant.parse("2026-04-20T00:00:00Z"));
        when(taskRepository.getLatestRootCheckpoint(taskId, tenantId)).thenReturn(Optional.of(Map.of(
                "checkpoint_id", "ckpt_1",
                "checkpoint_payload", payload,
                "created_at", created)));
        when(taskEventRepository.listEvents(eq(taskId), eq(tenantId), anyInt())).thenReturn(List.of());

        List<ActivityEventResponse> events = service.getActivity(taskId, false).events();
        assertEquals(1, events.size());
        assertEquals("turn.tool", events.get(0).kind());
        assertEquals(98765L, events.get(0).origBytes());
    }

    @Test
    void getActivity_toolTurn_noOrigBytes_leavesFieldNull() {
        UUID taskId = UUID.randomUUID();
        String tenantId = ValidationConstants.DEFAULT_TENANT_ID;

        String payload = """
                {"channel_values":{"messages":[
                  {"lc":1,"type":"constructor","id":["x","ToolMessage"],
                   "kwargs":{"type":"tool","content":"short","name":"grep",
                             "tool_call_id":"call_x","status":"success",
                             "additional_kwargs":{"emitted_at":"2026-04-20T00:00:00+00:00"}}}
                ]}}
                """;

        Timestamp created = Timestamp.from(Instant.parse("2026-04-20T00:00:00Z"));
        when(taskRepository.getLatestRootCheckpoint(taskId, tenantId)).thenReturn(Optional.of(Map.of(
                "checkpoint_id", "ckpt_1",
                "checkpoint_payload", payload,
                "created_at", created)));
        when(taskEventRepository.listEvents(eq(taskId), eq(tenantId), anyInt())).thenReturn(List.of());

        List<ActivityEventResponse> events = service.getActivity(taskId, false).events();
        assertEquals(1, events.size());
        assertNull(events.get(0).origBytes());
    }

    // --- Page.truncated flag ---

    @Test
    void getActivity_underMaxEvents_truncatedIsNullOrFalse() {
        UUID taskId = UUID.randomUUID();
        String tenantId = ValidationConstants.DEFAULT_TENANT_ID;

        String payload = """
                {"channel_values":{"messages":[
                  {"lc":1,"type":"constructor","id":["x","HumanMessage"],
                   "kwargs":{"type":"human","content":"hi",
                             "additional_kwargs":{"emitted_at":"2026-04-20T00:00:00+00:00"}}}
                ]}}
                """;
        Timestamp created = Timestamp.from(Instant.parse("2026-04-20T00:00:00Z"));
        when(taskRepository.getLatestRootCheckpoint(taskId, tenantId)).thenReturn(Optional.of(Map.of(
                "checkpoint_id", "ckpt_1",
                "checkpoint_payload", payload,
                "created_at", created)));
        when(taskEventRepository.listEvents(eq(taskId), eq(tenantId), anyInt())).thenReturn(List.of());

        ActivityEventResponse.Page page = service.getActivity(taskId, false);
        // Either null (omitted by @JsonInclude(NON_NULL)) or explicit false.
        assertTrue(page.truncated() == null || !page.truncated());
    }

    @Test
    void getActivity_overMaxEvents_truncatedIsTrueAndTrimmed() {
        UUID taskId = UUID.randomUUID();
        String tenantId = ValidationConstants.DEFAULT_TENANT_ID;

        // Synthesize MAX_EVENTS + 1 HumanMessages so the projection exceeds
        // the hard cap and we can assert both the truncation flag and the
        // trimmed list size.
        int count = ActivityProjectionService.MAX_EVENTS + 1;
        StringBuilder messages = new StringBuilder();
        for (int i = 0; i < count; i++) {
            if (i > 0) messages.append(",");
            // Each message gets a unique emitted_at so sort is deterministic.
            String ts = String.format("2026-04-20T00:00:00.%06d+00:00", i);
            messages.append("{\"lc\":1,\"type\":\"constructor\",\"id\":[\"x\",\"HumanMessage\"],")
                    .append("\"kwargs\":{\"type\":\"human\",\"content\":\"m").append(i).append("\",")
                    .append("\"id\":\"m_").append(i).append("\",")
                    .append("\"additional_kwargs\":{\"emitted_at\":\"").append(ts).append("\"}}}");
        }
        String payload = "{\"channel_values\":{\"messages\":[" + messages + "]}}";

        Timestamp created = Timestamp.from(Instant.parse("2026-04-20T00:00:00Z"));
        when(taskRepository.getLatestRootCheckpoint(taskId, tenantId)).thenReturn(Optional.of(Map.of(
                "checkpoint_id", "ckpt_1",
                "checkpoint_payload", payload,
                "created_at", created)));
        when(taskEventRepository.listEvents(eq(taskId), eq(tenantId), anyInt())).thenReturn(List.of());

        ActivityEventResponse.Page page = service.getActivity(taskId, false);
        assertEquals(Boolean.TRUE, page.truncated());
        assertEquals(ActivityProjectionService.MAX_EVENTS, page.events().size());
    }

    // --- Provider-shape normalization (fixture-driven) ---
    //
    // Each fixture drives an AIMessage whose ``content`` is the provider
    // shape under test; the projection must surface the expected prose on
    // ``turn.assistant.content``. Fixture IDs mirror the Python shared set.

    private static Stream<Arguments> providerContentFixtures() {
        return Stream.of(
                Arguments.of("F-STR-SIMPLE", "Hello world", "Hello world"),
                Arguments.of("F-STR-EMPTY", "", ""),
                Arguments.of("F-NULL", null, ""),
                Arguments.of("F-ANTHROPIC-PROSE",
                        List.of(Map.of("type", "text", "text", "Let me search for that")),
                        "Let me search for that"),
                Arguments.of("F-ANTHROPIC-MIXED",
                        List.of(
                                Map.of("type", "text", "text", "Sure, I'll check"),
                                Map.of("type", "tool_use", "id", "tu_1",
                                        "name", "web_search",
                                        "input", Map.of("q", "x"))),
                        "Sure, I'll check"),
                Arguments.of("F-ANTHROPIC-TOOLS-ONLY",
                        List.of(Map.of("type", "tool_use", "id", "tu_1",
                                "name", "web_search",
                                "input", Map.of("q", "x"))),
                        ""),
                Arguments.of("F-ANTHROPIC-THINKING",
                        List.of(
                                Map.of("type", "thinking",
                                        "thinking", "Deliberating...",
                                        "signature", "..."),
                                Map.of("type", "text", "text", "Here is the answer")),
                        "Deliberating...\n\nHere is the answer"),
                Arguments.of("F-OPENAI-NATIVE-OUTPUT-TEXT",
                        List.of(Map.of("type", "output_text", "text", "Here is the report")),
                        "Here is the report"),
                Arguments.of("F-OPENAI-NESTED-MESSAGE",
                        List.of(
                                Map.of("id", "rs_1", "type", "reasoning", "summary", List.of()),
                                Map.of("id", "msg_1", "type", "message",
                                        "content", List.of(Map.of(
                                                "type", "output_text",
                                                "text", "Below is a summary"))),
                                Map.of("id", "fc_1", "type", "function_call",
                                        "name", "web_search",
                                        "arguments", "{}")),
                        "Below is a summary"),
                Arguments.of("F-OPENAI-REASONING-ONLY",
                        List.of(
                                Map.of("id", "rs_1", "type", "reasoning", "summary", List.of()),
                                Map.of("id", "fc_1", "type", "function_call",
                                        "name", "web_search",
                                        "arguments", "{}")),
                        ""),
                Arguments.of("F-GEMINI-BARE-DICT",
                        List.of(Map.of("text", "Response from Gemini")),
                        "Response from Gemini"),
                Arguments.of("F-BEDROCK-CONVERSE-TEXT",
                        List.of(
                                Map.of("text", "Response via Bedrock"),
                                Map.of("toolUse", Map.of(
                                        "name", "search",
                                        "input", Map.of(),
                                        "toolUseId", "tu_1"))),
                        "Response via Bedrock"),
                Arguments.of("F-MULTI-TEXT-JOIN",
                        List.of(
                                Map.of("type", "text", "text", "First para"),
                                Map.of("type", "text", "text", "Second para")),
                        "First para\n\nSecond para")
        );
    }

    @ParameterizedTest(name = "{0}")
    @MethodSource("providerContentFixtures")
    void getActivity_normalizesAssistantContentForProviderShape(
            String fixtureId, Object aiContent, String expectedContent) {
        UUID taskId = UUID.randomUUID();
        String tenantId = ValidationConstants.DEFAULT_TENANT_ID;

        Map<String, Object> aiKwargs = new java.util.HashMap<>();
        aiKwargs.put("type", "ai");
        aiKwargs.put("content", aiContent);
        aiKwargs.put("id", "ai_1");

        Map<String, Object> aiMessage = Map.of(
                "lc", 1,
                "type", "constructor",
                "id", List.of("x", "y", "z", "AIMessage"),
                "kwargs", aiKwargs);
        Map<String, Object> payload = Map.of(
                "channel_values", Map.of("messages", List.of(aiMessage)));

        Timestamp created = Timestamp.from(Instant.parse("2026-04-20T00:00:00Z"));
        Map<String, Object> row = new java.util.HashMap<>();
        row.put("checkpoint_id", "ckpt_1");
        row.put("checkpoint_payload", payload);
        row.put("created_at", created);
        when(taskRepository.getLatestRootCheckpoint(taskId, tenantId))
                .thenReturn(Optional.of(row));
        when(taskEventRepository.listEvents(eq(taskId), eq(tenantId), anyInt()))
                .thenReturn(List.of());

        List<ActivityEventResponse> events = service.getActivity(taskId, false).events();
        assertEquals(1, events.size());
        assertEquals("turn.assistant", events.get(0).kind());
        assertEquals(expectedContent, events.get(0).content(),
                "fixture " + fixtureId);
    }

    @Test
    void getActivity_endToEnd_openAiReasoningPlusFunctionCallPlusNestedOutputText() {
        UUID taskId = UUID.randomUUID();
        String tenantId = ValidationConstants.DEFAULT_TENANT_ID;

        // Mirror a real OpenAI Responses turn: a reasoning block, a
        // function_call block, and a nested message with an output_text
        // child. Only the nested output_text prose should surface on
        // turn.assistant.content.
        List<Map<String, Object>> openAiContent = List.of(
                Map.of("id", "rs_1", "type", "reasoning", "summary", List.of()),
                Map.of("id", "fc_1", "type", "function_call",
                        "name", "web_search", "arguments", "{}"),
                Map.of("id", "msg_1", "type", "message",
                        "content", List.of(Map.of(
                                "type", "output_text",
                                "text", "Below is a summary"))));

        Map<String, Object> aiKwargs = new java.util.HashMap<>();
        aiKwargs.put("type", "ai");
        aiKwargs.put("content", openAiContent);
        aiKwargs.put("id", "ai_1");
        aiKwargs.put("tool_calls", List.of(Map.of(
                "id", "fc_1",
                "name", "web_search",
                "args", Map.of())));

        Map<String, Object> payload = Map.of(
                "channel_values", Map.of("messages", List.of(Map.of(
                        "lc", 1,
                        "type", "constructor",
                        "id", List.of("x", "y", "z", "AIMessage"),
                        "kwargs", aiKwargs))));

        Timestamp created = Timestamp.from(Instant.parse("2026-04-20T00:00:00Z"));
        Map<String, Object> row = new java.util.HashMap<>();
        row.put("checkpoint_id", "ckpt_1");
        row.put("checkpoint_payload", payload);
        row.put("created_at", created);
        when(taskRepository.getLatestRootCheckpoint(taskId, tenantId))
                .thenReturn(Optional.of(row));
        when(taskEventRepository.listEvents(eq(taskId), eq(tenantId), anyInt()))
                .thenReturn(List.of());

        List<ActivityEventResponse> events = service.getActivity(taskId, false).events();
        assertEquals(1, events.size());
        ActivityEventResponse turn = events.get(0);
        assertEquals("turn.assistant", turn.kind());
        assertEquals("Below is a summary", turn.content());
        assertNotNull(turn.toolCalls());
        assertEquals(1, turn.toolCalls().size());
        assertEquals("web_search", turn.toolCalls().get(0).name());
    }
}
