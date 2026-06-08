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

    // =========================================================================
    // S9 — Sub-agent fan-out observability markers
    // =========================================================================

    // --- mapMarker for each new event_type ---

    @Test
    void mapMarker_subagentStarted_mapsToMarkerSubagentStarted() {
        UUID taskId = UUID.randomUUID();
        String tenantId = ValidationConstants.DEFAULT_TENANT_ID;

        when(taskRepository.getLatestRootCheckpoint(taskId, tenantId)).thenReturn(Optional.empty());
        // No findByIdAndTenant stub needed: markerRows is non-empty, so the 404 path is not reached.

        TaskEventResponse event = new TaskEventResponse(
                UUID.randomUUID(), taskId, "agent-1", "subagent_started",
                null, null, "worker-1", null, null,
                Map.of("iteration", 1, "subtask", "1.0",
                        "prompt_preview", "search for X", "tool_allowlist", List.of("web_search"),
                        "depth", 1),
                OffsetDateTime.parse("2026-06-08T10:00:00+00:00"));
        when(taskEventRepository.listEvents(eq(taskId), eq(tenantId), anyInt()))
                .thenReturn(List.of(event));

        // include_details=true: marker.subagent.started is visible
        List<ActivityEventResponse> allEvents = service.getActivity(taskId, true).events();
        assertEquals(1, allEvents.size());
        ActivityEventResponse marker = allEvents.get(0);
        assertEquals("marker.subagent.started", marker.kind());
        assertEquals("subagent_started", marker.eventType());
        assertEquals(1, marker.iteration());
        assertEquals("1.0", marker.subtask());
        assertNotNull(marker.details());

        // include_details=false: marker.subagent.started is EXCLUDED (lifecycle telemetry)
        List<ActivityEventResponse> userVisible = service.getActivity(taskId, false).events();
        assertTrue(userVisible.isEmpty(),
                "marker.subagent.started should be hidden when include_details=false");
    }

    @Test
    void mapMarker_subagentFinding_mapsToMarkerSubagentFinding() {
        UUID taskId = UUID.randomUUID();
        String tenantId = ValidationConstants.DEFAULT_TENANT_ID;

        when(taskRepository.getLatestRootCheckpoint(taskId, tenantId)).thenReturn(Optional.empty());

        TaskEventResponse event = new TaskEventResponse(
                UUID.randomUUID(), taskId, "agent-1", "subagent_finding",
                null, null, "worker-1", null, null,
                Map.of("iteration", 0, "subtask", "0.1",
                        "finding_id", "0.1-abc12345", "source_url", "https://example.com/doc"),
                OffsetDateTime.parse("2026-06-08T10:01:00+00:00"));
        when(taskEventRepository.listEvents(eq(taskId), eq(tenantId), anyInt()))
                .thenReturn(List.of(event));

        // include_details=true
        List<ActivityEventResponse> allEvents = service.getActivity(taskId, true).events();
        assertEquals(1, allEvents.size());
        ActivityEventResponse marker = allEvents.get(0);
        assertEquals("marker.subagent.finding", marker.kind());
        assertEquals(0, marker.iteration());
        assertEquals("0.1", marker.subtask());

        // include_details=false: marker.subagent.finding IS user-visible
        List<ActivityEventResponse> userVisible = service.getActivity(taskId, false).events();
        assertEquals(1, userVisible.size());
        assertEquals("marker.subagent.finding", userVisible.get(0).kind());
    }

    @Test
    void mapMarker_subagentFailed_mapsToMarkerSubagentFailed() {
        UUID taskId = UUID.randomUUID();
        String tenantId = ValidationConstants.DEFAULT_TENANT_ID;

        when(taskRepository.getLatestRootCheckpoint(taskId, tenantId)).thenReturn(Optional.empty());

        TaskEventResponse event = new TaskEventResponse(
                UUID.randomUUID(), taskId, "agent-1", "subagent_failed",
                null, null, "worker-1", null, null,
                Map.of("iteration", 2, "subtask", "2.0", "reason", "ceiling"),
                OffsetDateTime.parse("2026-06-08T10:02:00+00:00"));
        when(taskEventRepository.listEvents(eq(taskId), eq(tenantId), anyInt()))
                .thenReturn(List.of(event));

        // include_details=true
        List<ActivityEventResponse> allEvents = service.getActivity(taskId, true).events();
        assertEquals(1, allEvents.size());
        ActivityEventResponse marker = allEvents.get(0);
        assertEquals("marker.subagent.failed", marker.kind());
        assertEquals(2, marker.iteration());
        assertEquals("2.0", marker.subtask());

        // include_details=false: marker.subagent.failed IS user-visible
        List<ActivityEventResponse> userVisible = service.getActivity(taskId, false).events();
        assertEquals(1, userVisible.size());
        assertEquals("marker.subagent.failed", userVisible.get(0).kind());
    }

    @Test
    void mapMarker_supervisorIteration_mapsToMarkerSupervisorIteration() {
        UUID taskId = UUID.randomUUID();
        String tenantId = ValidationConstants.DEFAULT_TENANT_ID;

        when(taskRepository.getLatestRootCheckpoint(taskId, tenantId)).thenReturn(Optional.empty());

        TaskEventResponse event = new TaskEventResponse(
                UUID.randomUUID(), taskId, "agent-1", "supervisor_iteration",
                null, null, "worker-1", null, null,
                Map.of("iteration", 1, "subtasks_emitted", 3,
                        "decision", "continue", "reason", "not enough findings yet"),
                OffsetDateTime.parse("2026-06-08T10:03:00+00:00"));
        when(taskEventRepository.listEvents(eq(taskId), eq(tenantId), anyInt()))
                .thenReturn(List.of(event));

        // include_details=true
        List<ActivityEventResponse> allEvents = service.getActivity(taskId, true).events();
        assertEquals(1, allEvents.size());
        ActivityEventResponse marker = allEvents.get(0);
        assertEquals("marker.supervisor.iteration", marker.kind());
        assertEquals(1, marker.iteration());
        // supervisor_iteration has no per-sub-agent subtask
        assertNull(marker.subtask());

        // include_details=false: marker.supervisor.iteration IS user-visible
        List<ActivityEventResponse> userVisible = service.getActivity(taskId, false).events();
        assertEquals(1, userVisible.size());
        assertEquals("marker.supervisor.iteration", userVisible.get(0).kind());
    }

    // --- Missing / malformed iteration guard ---

    @Test
    void mapMarker_subagentStarted_missingIteration_iterationIsNull() {
        UUID taskId = UUID.randomUUID();
        String tenantId = ValidationConstants.DEFAULT_TENANT_ID;

        when(taskRepository.getLatestRootCheckpoint(taskId, tenantId)).thenReturn(Optional.empty());

        // details map has no "iteration" key — guard must not throw
        TaskEventResponse event = new TaskEventResponse(
                UUID.randomUUID(), taskId, "agent-1", "subagent_started",
                null, null, "worker-1", null, null,
                Map.of("subtask", "1.0", "prompt_preview", "do X"),
                OffsetDateTime.parse("2026-06-08T10:04:00+00:00"));
        when(taskEventRepository.listEvents(eq(taskId), eq(tenantId), anyInt()))
                .thenReturn(List.of(event));

        List<ActivityEventResponse> allEvents = service.getActivity(taskId, true).events();
        assertEquals(1, allEvents.size());
        ActivityEventResponse marker = allEvents.get(0);
        assertEquals("marker.subagent.started", marker.kind());
        assertNull(marker.iteration(), "iteration must be null when missing from details");
    }

    @Test
    void mapMarker_subagentStarted_nonNumberIteration_iterationIsNull() {
        UUID taskId = UUID.randomUUID();
        String tenantId = ValidationConstants.DEFAULT_TENANT_ID;

        when(taskRepository.getLatestRootCheckpoint(taskId, tenantId)).thenReturn(Optional.empty());

        // details map has iteration as a non-Number — guard must not throw
        Map<String, Object> details = new java.util.HashMap<>();
        details.put("iteration", "not-a-number");
        details.put("subtask", "1.0");
        TaskEventResponse event = new TaskEventResponse(
                UUID.randomUUID(), taskId, "agent-1", "subagent_started",
                null, null, "worker-1", null, null,
                details,
                OffsetDateTime.parse("2026-06-08T10:05:00+00:00"));
        when(taskEventRepository.listEvents(eq(taskId), eq(tenantId), anyInt()))
                .thenReturn(List.of(event));

        List<ActivityEventResponse> allEvents = service.getActivity(taskId, true).events();
        assertEquals(1, allEvents.size());
        assertNull(allEvents.get(0).iteration(),
                "iteration must be null when details value is not a Number");
    }

    // --- include_details=true/false filter for all four new kinds ---

    @Test
    void getActivity_subagentMarkers_includeDetailsFilter() {
        UUID taskId = UUID.randomUUID();
        String tenantId = ValidationConstants.DEFAULT_TENANT_ID;

        when(taskRepository.getLatestRootCheckpoint(taskId, tenantId)).thenReturn(Optional.empty());

        TaskEventResponse started = new TaskEventResponse(
                UUID.randomUUID(), taskId, "a", "subagent_started",
                null, null, "w", null, null,
                Map.of("iteration", 0, "subtask", "0.0", "prompt_preview", "p", "depth", 1),
                OffsetDateTime.parse("2026-06-08T10:00:00+00:00"));
        TaskEventResponse finding = new TaskEventResponse(
                UUID.randomUUID(), taskId, "a", "subagent_finding",
                null, null, "w", null, null,
                Map.of("iteration", 0, "subtask", "0.0",
                        "finding_id", "0.0-aabb1234", "source_url", "https://x.com"),
                OffsetDateTime.parse("2026-06-08T10:01:00+00:00"));
        TaskEventResponse failed = new TaskEventResponse(
                UUID.randomUUID(), taskId, "a", "subagent_failed",
                null, null, "w", null, null,
                Map.of("iteration", 0, "subtask", "0.1", "reason", "timeout"),
                OffsetDateTime.parse("2026-06-08T10:02:00+00:00"));
        TaskEventResponse iteration = new TaskEventResponse(
                UUID.randomUUID(), taskId, "a", "supervisor_iteration",
                null, null, "w", null, null,
                Map.of("iteration", 0, "subtasks_emitted", 2, "decision", "stop", "reason", "done"),
                OffsetDateTime.parse("2026-06-08T10:03:00+00:00"));

        when(taskEventRepository.listEvents(eq(taskId), eq(tenantId), anyInt()))
                .thenReturn(List.of(started, finding, failed, iteration));

        // include_details=true → all four visible
        List<ActivityEventResponse> all = service.getActivity(taskId, true).events();
        List<String> allKinds = all.stream().map(ActivityEventResponse::kind).toList();
        assertTrue(allKinds.contains("marker.subagent.started"));
        assertTrue(allKinds.contains("marker.subagent.finding"));
        assertTrue(allKinds.contains("marker.subagent.failed"));
        assertTrue(allKinds.contains("marker.supervisor.iteration"));

        // include_details=false → started excluded, the other three visible
        List<ActivityEventResponse> userVisible = service.getActivity(taskId, false).events();
        List<String> userKinds = userVisible.stream().map(ActivityEventResponse::kind).toList();
        assertFalse(userKinds.contains("marker.subagent.started"),
                "marker.subagent.started must be hidden when include_details=false");
        assertTrue(userKinds.contains("marker.subagent.finding"));
        assertTrue(userKinds.contains("marker.subagent.failed"));
        assertTrue(userKinds.contains("marker.supervisor.iteration"));
    }

    // --- Duplicate-tolerance (at-least-once contract) ---

    @Test
    void getActivity_duplicateSubagentStarted_firstWinsDedup() {
        // Two rows with same (event_type, iteration, subtask) — first-wins for subagent_started.
        // The final projected list should contain exactly ONE entry with the first row's data.
        UUID taskId = UUID.randomUUID();
        String tenantId = ValidationConstants.DEFAULT_TENANT_ID;

        when(taskRepository.getLatestRootCheckpoint(taskId, tenantId)).thenReturn(Optional.empty());

        TaskEventResponse first = new TaskEventResponse(
                UUID.randomUUID(), taskId, "a", "subagent_started",
                null, null, "w", null, null,
                Map.of("iteration", 1, "subtask", "1.0", "prompt_preview", "FIRST", "depth", 1),
                OffsetDateTime.parse("2026-06-08T10:00:00+00:00"));
        // Duplicate row (re-emitted on resume) — same dedup key, different prompt_preview
        TaskEventResponse duplicate = new TaskEventResponse(
                UUID.randomUUID(), taskId, "a", "subagent_started",
                null, null, "w", null, null,
                Map.of("iteration", 1, "subtask", "1.0", "prompt_preview", "DUPLICATE", "depth", 1),
                OffsetDateTime.parse("2026-06-08T10:00:01+00:00"));

        when(taskEventRepository.listEvents(eq(taskId), eq(tenantId), anyInt()))
                .thenReturn(List.of(first, duplicate));

        List<ActivityEventResponse> events = service.getActivity(taskId, true).events();
        // Must deduplicate to exactly one entry
        assertEquals(1, events.size(),
                "Duplicate subagent_started rows with same (event_type, iteration, subtask) must deduplicate to 1");
        ActivityEventResponse marker = events.get(0);
        assertEquals("marker.subagent.started", marker.kind());
        assertEquals(1, marker.iteration());
        assertEquals("1.0", marker.subtask());
        // first-wins: the FIRST row's details should survive
        @SuppressWarnings("unchecked")
        Map<String, Object> details = (Map<String, Object>) marker.details();
        assertEquals("FIRST", details.get("prompt_preview"),
                "first-wins dedup: first row's prompt_preview must survive");
    }

    @Test
    void getActivity_duplicateSubagentFailed_lastWinsDedup() {
        // Two rows with same (event_type, iteration, subtask) — last-wins for subagent_failed.
        UUID taskId = UUID.randomUUID();
        String tenantId = ValidationConstants.DEFAULT_TENANT_ID;

        when(taskRepository.getLatestRootCheckpoint(taskId, tenantId)).thenReturn(Optional.empty());

        TaskEventResponse first = new TaskEventResponse(
                UUID.randomUUID(), taskId, "a", "subagent_failed",
                null, null, "w", null, null,
                Map.of("iteration", 0, "subtask", "0.0", "reason", "timeout"),
                OffsetDateTime.parse("2026-06-08T10:00:00+00:00"));
        // Duplicate with updated reason (re-emit on resume)
        TaskEventResponse last = new TaskEventResponse(
                UUID.randomUUID(), taskId, "a", "subagent_failed",
                null, null, "w", null, null,
                Map.of("iteration", 0, "subtask", "0.0", "reason", "error"),
                OffsetDateTime.parse("2026-06-08T10:00:01+00:00"));

        when(taskEventRepository.listEvents(eq(taskId), eq(tenantId), anyInt()))
                .thenReturn(List.of(first, last));

        List<ActivityEventResponse> events = service.getActivity(taskId, true).events();
        assertEquals(1, events.size(),
                "Duplicate subagent_failed rows with same (event_type, iteration, subtask) must deduplicate to 1");
        @SuppressWarnings("unchecked")
        Map<String, Object> details = (Map<String, Object>) events.get(0).details();
        assertEquals("error", details.get("reason"),
                "last-wins dedup: last row's reason must survive");
    }

    @Test
    void getActivity_supervisorIterationDuplicate_lastWinsDedup() {
        // supervisor_iteration also uses last-wins dedup.
        UUID taskId = UUID.randomUUID();
        String tenantId = ValidationConstants.DEFAULT_TENANT_ID;

        when(taskRepository.getLatestRootCheckpoint(taskId, tenantId)).thenReturn(Optional.empty());

        TaskEventResponse first = new TaskEventResponse(
                UUID.randomUUID(), taskId, "a", "supervisor_iteration",
                null, null, "w", null, null,
                Map.of("iteration", 2, "subtasks_emitted", 2, "decision", "continue", "reason", "old"),
                OffsetDateTime.parse("2026-06-08T10:00:00+00:00"));
        TaskEventResponse last = new TaskEventResponse(
                UUID.randomUUID(), taskId, "a", "supervisor_iteration",
                null, null, "w", null, null,
                Map.of("iteration", 2, "subtasks_emitted", 2, "decision", "stop", "reason", "updated"),
                OffsetDateTime.parse("2026-06-08T10:00:01+00:00"));

        when(taskEventRepository.listEvents(eq(taskId), eq(tenantId), anyInt()))
                .thenReturn(List.of(first, last));

        List<ActivityEventResponse> events = service.getActivity(taskId, true).events();
        assertEquals(1, events.size(),
                "Duplicate supervisor_iteration rows must deduplicate to 1");
        @SuppressWarnings("unchecked")
        Map<String, Object> details = (Map<String, Object>) events.get(0).details();
        assertEquals("stop", details.get("decision"),
                "last-wins dedup: last row's decision must survive");
        assertEquals("updated", details.get("reason"));
    }

    // --- Regression: existing markers project unchanged ---

    @Test
    void getActivity_existingMarkers_projectUnchangedWithNullIterationSubtask() {
        // Verifies that the two new iteration/subtask fields are absent (null)
        // on pre-existing marker kinds — serialise byte-identically.
        UUID taskId = UUID.randomUUID();
        String tenantId = ValidationConstants.DEFAULT_TENANT_ID;

        when(taskRepository.getLatestRootCheckpoint(taskId, tenantId)).thenReturn(Optional.empty());

        TaskEventResponse compaction = new TaskEventResponse(
                UUID.randomUUID(), taskId, "a", "task_compaction_fired",
                null, null, "w", null, null,
                Map.of("summary_text", "prior turns summarised", "tokens_in", 800),
                OffsetDateTime.parse("2026-06-08T10:00:00+00:00"));
        TaskEventResponse hitl = new TaskEventResponse(
                UUID.randomUUID(), taskId, "a", "task_paused",
                "running", "paused", "w", null, null,
                Map.of("reason", "tool_requires_approval"),
                OffsetDateTime.parse("2026-06-08T10:01:00+00:00"));
        TaskEventResponse lifecycle = new TaskEventResponse(
                UUID.randomUUID(), taskId, "a", "task_completed",
                "running", "completed", "w", null, null,
                Map.of(),
                OffsetDateTime.parse("2026-06-08T10:02:00+00:00"));

        when(taskEventRepository.listEvents(eq(taskId), eq(tenantId), anyInt()))
                .thenReturn(List.of(compaction, hitl, lifecycle));

        List<ActivityEventResponse> all = service.getActivity(taskId, true).events();
        assertEquals(3, all.size());

        ActivityEventResponse comp = all.stream()
                .filter(e -> "marker.compaction_fired".equals(e.kind())).findFirst().orElseThrow();
        assertNull(comp.iteration(), "iteration must be null on marker.compaction_fired");
        assertNull(comp.subtask(), "subtask must be null on marker.compaction_fired");
        assertEquals("prior turns summarised", comp.summaryText());

        ActivityEventResponse hitlMarker = all.stream()
                .filter(e -> "marker.hitl.paused".equals(e.kind())).findFirst().orElseThrow();
        assertNull(hitlMarker.iteration(), "iteration must be null on marker.hitl.paused");
        assertNull(hitlMarker.subtask(), "subtask must be null on marker.hitl.paused");

        ActivityEventResponse lc = all.stream()
                .filter(e -> "marker.lifecycle".equals(e.kind())).findFirst().orElseThrow();
        assertNull(lc.iteration(), "iteration must be null on marker.lifecycle");
        assertNull(lc.subtask(), "subtask must be null on marker.lifecycle");
    }

    // --- Unknown event_type is dropped (forward-compat) ---

    @Test
    void getActivity_unknownEventType_isDroppedNotErrored() {
        // A row written by a newer worker with an event_type the API doesn't know
        // must be silently dropped — not throw an exception.
        UUID taskId = UUID.randomUUID();
        String tenantId = ValidationConstants.DEFAULT_TENANT_ID;

        when(taskRepository.getLatestRootCheckpoint(taskId, tenantId)).thenReturn(Optional.empty());
        // markerRows is non-empty (the unknown event row), so 404 path is not reached
        // — no findByIdAndTenant stub needed.

        TaskEventResponse unknown = new TaskEventResponse(
                UUID.randomUUID(), taskId, "a", "future_event_type_unknown_to_this_api",
                null, null, "w", null, null,
                Map.of("some_key", "some_value"),
                OffsetDateTime.parse("2026-06-08T10:00:00+00:00"));
        when(taskEventRepository.listEvents(eq(taskId), eq(tenantId), anyInt()))
                .thenReturn(List.of(unknown));

        // Must return empty events list — no exception thrown
        List<ActivityEventResponse> events = service.getActivity(taskId, true).events();
        assertTrue(events.isEmpty(),
                "Unknown event_type must be silently dropped, not errored");
    }
}
