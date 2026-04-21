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
}
