package com.persistentagent.api.service;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.SerializationFeature;
import com.fasterxml.jackson.datatype.jsr310.JavaTimeModule;
import com.persistentagent.api.exception.AgentNotFoundException;
import com.persistentagent.api.exception.InvalidStateTransitionException;
import com.persistentagent.api.exception.TaskNotFoundException;
import com.persistentagent.api.exception.ValidationException;
import com.persistentagent.api.model.request.TaskSubmissionRequest;
import com.persistentagent.api.model.response.*;
import com.persistentagent.api.repository.AgentRepository;
import com.persistentagent.api.repository.ArtifactRepository;
import com.persistentagent.api.repository.LangfuseEndpointRepository;
import com.persistentagent.api.repository.ModelRepository;
import com.persistentagent.api.repository.TaskAttachedMemoryRepository;
import com.persistentagent.api.repository.TaskRepository;
import com.persistentagent.api.repository.TaskRepository.MutationResult;
import com.persistentagent.api.repository.TaskRepository.CancelResult;
import com.persistentagent.api.repository.TaskRepository.RedriveResult;
import com.persistentagent.api.service.observability.CheckpointCostTotals;
import com.persistentagent.api.service.observability.TaskObservabilityService;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.ArgumentCaptor;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;

import java.sql.Timestamp;
import java.time.Instant;
import java.time.OffsetDateTime;
import java.util.*;

import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.ArgumentMatchers.*;
import static org.mockito.Mockito.*;

@ExtendWith(MockitoExtension.class)
class TaskServiceTest {

    @Mock
    private ArtifactRepository artifactRepository;

    @Mock
    private TaskRepository taskRepository;

    @Mock
    private AgentRepository agentRepository;

    @Mock
    private ModelRepository modelRepository;

    @Mock
    private LangfuseEndpointRepository langfuseEndpointRepository;

    @Mock
    private TaskObservabilityService taskObservabilityService;

    @Mock
    private TaskEventService taskEventService;

    @Mock
    private ConfigValidationHelper configValidationHelper;

    @Mock
    private S3StorageService s3StorageService;

    @Mock
    private TaskAttachedMemoryRepository taskAttachedMemoryRepository;

    private TaskService taskService;
    private ObjectMapper objectMapper;

    @BeforeEach
    void setUp() {
        objectMapper = new ObjectMapper();
        objectMapper.registerModule(new JavaTimeModule());
        objectMapper.disable(SerializationFeature.WRITE_DATES_AS_TIMESTAMPS);
        taskService = new TaskService(
                artifactRepository,
                taskRepository,
                agentRepository,
                modelRepository,
                langfuseEndpointRepository,
                taskObservabilityService,
                taskEventService,
                objectMapper,
                new CheckpointEventParser(objectMapper),
                configValidationHelper,
                s3StorageService,
                taskAttachedMemoryRepository,
                false
        );
    }

    // --- submitTask tests ---

    @Test
    void submitTask_validRequest_returnsCreated() {
        TaskSubmissionRequest request = new TaskSubmissionRequest(
                null, "agent1", "do something", 3, 100, 3600, null, null, null);

        UUID taskId = UUID.randomUUID();
        Timestamp now = Timestamp.from(Instant.now());
        Map<String, Object> inserted = new LinkedHashMap<>();
        inserted.put("task_id", taskId);
        inserted.put("agent_display_name_snapshot", "Agent One");
        inserted.put("created_at", now);
        when(taskRepository.insertTaskFromAgent(anyString(), eq("agent1"), anyString(),
                eq("do something"), eq(3), eq(100), eq(3600), isNull(), anyString()))
                .thenReturn(Optional.of(inserted));

        TaskSubmissionResponse response = taskService.submitTask(request);

        assertNotNull(response);
        assertEquals(taskId, response.taskId());
        assertEquals("agent1", response.agentId());
        assertEquals("Agent One", response.agentDisplayName());
        assertEquals("queued", response.status());
        assertNotNull(response.createdAt());
    }

    @Test
    void submitTask_withValidLangfuseEndpointId_succeeds() {
        UUID endpointId = UUID.randomUUID();
        TaskSubmissionRequest request = new TaskSubmissionRequest(
                null, "agent1", "do something", 3, 100, 3600, endpointId, null, null);

        UUID taskId = UUID.randomUUID();
        Timestamp now = Timestamp.from(Instant.now());
        Map<String, Object> endpointRow = Map.of("endpoint_id", endpointId);
        when(langfuseEndpointRepository.findByIdAndTenant(endpointId, "default"))
                .thenReturn(Optional.of(endpointRow));

        Map<String, Object> inserted = new LinkedHashMap<>();
        inserted.put("task_id", taskId);
        inserted.put("agent_display_name_snapshot", "Agent One");
        inserted.put("created_at", now);
        when(taskRepository.insertTaskFromAgent(anyString(), eq("agent1"), anyString(),
                eq("do something"), eq(3), eq(100), eq(3600), eq(endpointId), anyString()))
                .thenReturn(Optional.of(inserted));

        TaskSubmissionResponse response = taskService.submitTask(request);

        assertNotNull(response);
        assertEquals(taskId, response.taskId());
    }

    @Test
    void submitTask_withInvalidLangfuseEndpointId_throwsValidation() {
        UUID endpointId = UUID.randomUUID();
        TaskSubmissionRequest request = new TaskSubmissionRequest(
                null, "agent1", "do something", 3, 100, 3600, endpointId, null, null);

        when(langfuseEndpointRepository.findByIdAndTenant(endpointId, "default"))
                .thenReturn(Optional.empty());

        assertThrows(ValidationException.class, () -> taskService.submitTask(request));
    }

    @Test
    void submitTask_agentNotFound_throwsAgentNotFoundException() {
        TaskSubmissionRequest request = new TaskSubmissionRequest(
                null, "agent-unknown", "input", null, null, null, null, null, null);

        when(taskRepository.insertTaskFromAgent(anyString(), eq("agent-unknown"), anyString(),
                eq("input"), anyInt(), anyInt(), anyInt(), isNull(), anyString()))
                .thenReturn(Optional.empty());
        when(agentRepository.findByIdAndTenant("default", "agent-unknown"))
                .thenReturn(Optional.empty());

        assertThrows(AgentNotFoundException.class, () -> taskService.submitTask(request));
    }

    @Test
    void submitTask_disabledAgent_throwsValidation() {
        TaskSubmissionRequest request = new TaskSubmissionRequest(
                null, "agent-disabled", "input", null, null, null, null, null, null);

        when(taskRepository.insertTaskFromAgent(anyString(), eq("agent-disabled"), anyString(),
                eq("input"), anyInt(), anyInt(), anyInt(), isNull(), anyString()))
                .thenReturn(Optional.empty());
        Map<String, Object> agentRow = new LinkedHashMap<>();
        agentRow.put("agent_id", "agent-disabled");
        agentRow.put("status", "disabled");
        when(agentRepository.findByIdAndTenant("default", "agent-disabled"))
                .thenReturn(Optional.of(agentRow));

        ValidationException ex = assertThrows(ValidationException.class, () -> taskService.submitTask(request));
        assertTrue(ex.getMessage().contains("disabled"));
    }

    @Test
    void submitTask_modelDeactivated_throwsValidation() {
        TaskSubmissionRequest request = new TaskSubmissionRequest(
                null, "agent1", "input", null, null, null, null, null, null);

        when(taskRepository.insertTaskFromAgent(anyString(), eq("agent1"), anyString(),
                eq("input"), anyInt(), anyInt(), anyInt(), isNull(), anyString()))
                .thenReturn(Optional.empty());
        Map<String, Object> agentRow = new LinkedHashMap<>();
        agentRow.put("agent_id", "agent1");
        agentRow.put("status", "active");
        when(agentRepository.findByIdAndTenant("default", "agent1"))
                .thenReturn(Optional.of(agentRow));

        ValidationException ex = assertThrows(ValidationException.class, () -> taskService.submitTask(request));
        assertTrue(ex.getMessage().contains("model is no longer active"));
    }

    @Test
    void submitTask_shortTimeoutRejectedWhenDevTaskControlsDisabled() {
        TaskSubmissionRequest request = new TaskSubmissionRequest(
                null, "agent1", "input", null, null, 30, null, null, null);

        assertThrows(ValidationException.class, () -> taskService.submitTask(request));
    }

    @Test
    void submitTask_defaultValues_usedWhenNull() {
        TaskSubmissionRequest request = new TaskSubmissionRequest(
                null, "agent1", "input", null, null, null, null, null, null);

        UUID taskId = UUID.randomUUID();
        Timestamp now = Timestamp.from(Instant.now());
        Map<String, Object> inserted = new LinkedHashMap<>();
        inserted.put("task_id", taskId);
        inserted.put("agent_display_name_snapshot", "Agent One");
        inserted.put("created_at", now);
        when(taskRepository.insertTaskFromAgent(eq("default"), eq("agent1"), eq("shared"),
                eq("input"), eq(3), eq(100), eq(3600), isNull(), anyString()))
                .thenReturn(Optional.of(inserted));

        taskService.submitTask(request);

        verify(taskRepository).insertTaskFromAgent(eq("default"), eq("agent1"), eq("shared"),
                eq("input"), eq(3), eq(100), eq(3600), isNull(), anyString());
    }

    // --- submitTask: attached_memory_ids + memory_mode ---

    @Test
    void submitTask_withNullAttachedMemoryIds_insertsNoAttachmentRows() {
        TaskSubmissionRequest request = new TaskSubmissionRequest(
                null, "agent1", "input", null, null, null, null, null, null);

        UUID taskId = UUID.randomUUID();
        stubSuccessfulInsert(taskId);

        TaskSubmissionResponse response = taskService.submitTask(request);

        assertNotNull(response.attachedMemoryIds());
        assertTrue(response.attachedMemoryIds().isEmpty());
        assertNotNull(response.attachedMemoriesPreview());
        assertTrue(response.attachedMemoriesPreview().isEmpty());
        // When nothing is attached, we must not issue the scope-resolution query.
        verify(taskAttachedMemoryRepository, never()).resolveScopedMemoryIds(anyString(), anyString(), anyList());
        // findAttachedMemoriesPreview should also not run for an empty attach list.
        verify(taskAttachedMemoryRepository, never()).findAttachedMemoriesPreview(
                any(UUID.class), anyString(), anyString());
    }

    @Test
    void submitTask_withEmptyAttachedMemoryIds_treatedSameAsAbsent() {
        TaskSubmissionRequest request = new TaskSubmissionRequest(
                null, "agent1", "input", null, null, null, null, List.of(), null);

        UUID taskId = UUID.randomUUID();
        stubSuccessfulInsert(taskId);

        TaskSubmissionResponse response = taskService.submitTask(request);

        assertTrue(response.attachedMemoryIds().isEmpty());
        verify(taskAttachedMemoryRepository, never()).resolveScopedMemoryIds(anyString(), anyString(), anyList());
    }

    @Test
    void submitTask_withThreeValidAttachedMemoryIds_insertsJoinRowsAndMirrorsEvent() {
        UUID m1 = UUID.randomUUID();
        UUID m2 = UUID.randomUUID();
        UUID m3 = UUID.randomUUID();
        List<UUID> inputIds = List.of(m1, m2, m3);
        TaskSubmissionRequest request = new TaskSubmissionRequest(
                null, "agent1", "input", null, null, null, null, inputIds, null);

        UUID taskId = UUID.randomUUID();
        stubSuccessfulInsert(taskId);
        when(taskAttachedMemoryRepository.resolveScopedMemoryIds("default", "agent1", inputIds))
                .thenReturn(List.of(m1, m2, m3));

        TaskSubmissionResponse response = taskService.submitTask(request);

        assertEquals(inputIds, response.attachedMemoryIds());
        verify(taskAttachedMemoryRepository).insertAttachments(taskId, inputIds);
        ArgumentCaptor<String> detailsCaptor = ArgumentCaptor.forClass(String.class);
        verify(taskEventService).recordEvent(
                eq("default"), eq(taskId), eq("agent1"),
                eq("task_submitted"), isNull(), eq("queued"),
                isNull(), isNull(), isNull(), detailsCaptor.capture());
        String details = detailsCaptor.getValue();
        // Mirror the list in the event details JSONB
        assertTrue(details.contains(m1.toString()));
        assertTrue(details.contains(m2.toString()));
        assertTrue(details.contains(m3.toString()));
        assertTrue(details.contains("attached_memory_ids"));
    }

    @Test
    void submitTask_withResolutionMiss_rejectsUniformlyAndDoesNotPersistAttachments() {
        // Resolution runs AFTER the atomic agent-insert so unknown-agent errors win over
        // scope-miss errors. A resolution miss on a valid agent still fails the request
        // uniformly, and the @Transactional rollback (exercised by Spring at runtime)
        // discards the task row. The mock here only verifies the @code{insertAttachments}
        // side-effect is skipped and the task_submitted event is not emitted.
        UUID ok = UUID.randomUUID();
        UUID miss = UUID.randomUUID();
        UUID taskId = UUID.randomUUID();
        List<UUID> inputIds = List.of(ok, miss);
        TaskSubmissionRequest request = new TaskSubmissionRequest(
                null, "agent1", "input", null, null, null, null, inputIds, null);

        Map<String, Object> inserted = new HashMap<>();
        inserted.put("task_id", taskId);
        inserted.put("agent_display_name_snapshot", "agent1");
        inserted.put("created_at", OffsetDateTime.now());
        when(taskRepository.insertTaskFromAgent(anyString(), anyString(), anyString(),
                anyString(), anyInt(), anyInt(), anyInt(), any(), anyString()))
                .thenReturn(Optional.of(inserted));
        when(taskAttachedMemoryRepository.resolveScopedMemoryIds("default", "agent1", inputIds))
                .thenReturn(List.of(ok)); // count mismatch — one missed scope

        ValidationException ex = assertThrows(ValidationException.class,
                () -> taskService.submitTask(request));
        assertTrue(ex.getMessage().toLowerCase().contains("attached_memory_ids"),
                "Message should reference the field; got: " + ex.getMessage());
        String lower = ex.getMessage().toLowerCase();
        assertFalse(lower.contains("tenant") || lower.contains("agent_id")
                        || lower.contains(miss.toString().toLowerCase()),
                "Message must not leak scope-miss cause or the offending id: " + ex.getMessage());
        verify(taskAttachedMemoryRepository, never()).insertAttachments(any(UUID.class), anyList());
        verify(taskEventService, never()).recordEvent(anyString(), any(UUID.class), anyString(),
                anyString(), any(), anyString(), any(), any(), any(), anyString());
    }

    @Test
    void submitTask_withDuplicateAttachedMemoryIds_rejectsWith400() {
        UUID dup = UUID.randomUUID();
        List<UUID> ids = List.of(dup, dup);
        TaskSubmissionRequest request = new TaskSubmissionRequest(
                null, "agent1", "input", null, null, null, null, ids, null);

        ValidationException ex = assertThrows(ValidationException.class,
                () -> taskService.submitTask(request));
        assertTrue(ex.getMessage().toLowerCase().contains("duplicate"),
                "Duplicate-id rejection should say 'duplicate'; got: " + ex.getMessage());
        verify(taskRepository, never()).insertTaskFromAgent(anyString(), anyString(), anyString(),
                anyString(), anyInt(), anyInt(), anyInt(), any(), anyString());
    }

    @Test
    void submitTask_withOver50AttachedMemoryIds_rejectsWith400() {
        List<UUID> ids = new ArrayList<>();
        for (int i = 0; i < 51; i++) {
            ids.add(UUID.randomUUID());
        }
        TaskSubmissionRequest request = new TaskSubmissionRequest(
                null, "agent1", "input", null, null, null, null, ids, null);

        ValidationException ex = assertThrows(ValidationException.class,
                () -> taskService.submitTask(request));
        assertTrue(ex.getMessage().contains("50"),
                "Cap-exceeded rejection should mention 50; got: " + ex.getMessage());
        verify(taskRepository, never()).insertTaskFromAgent(anyString(), anyString(), anyString(),
                anyString(), anyInt(), anyInt(), anyInt(), any(), anyString());
    }

    @Test
    void submitTask_with50AttachedMemoryIds_accepted() {
        List<UUID> ids = new ArrayList<>();
        for (int i = 0; i < 50; i++) {
            ids.add(UUID.randomUUID());
        }
        TaskSubmissionRequest request = new TaskSubmissionRequest(
                null, "agent1", "input", null, null, null, null, ids, null);
        UUID taskId = UUID.randomUUID();
        stubSuccessfulInsert(taskId);
        when(taskAttachedMemoryRepository.resolveScopedMemoryIds(eq("default"), eq("agent1"), eq(ids)))
                .thenReturn(ids);

        assertDoesNotThrow(() -> taskService.submitTask(request));
        verify(taskAttachedMemoryRepository).insertAttachments(taskId, ids);
    }

    @Test
    void submitTask_withMemoryModeSkip_persistsOnTaskRow() {
        TaskSubmissionRequest request = new TaskSubmissionRequest(
                null, "agent1", "input", null, null, null, null, null, "skip");

        UUID taskId = UUID.randomUUID();
        stubSuccessfulInsertExpectingMemoryMode(taskId, "skip");

        taskService.submitTask(request);

        verify(taskRepository).insertTaskFromAgent(
                eq("default"), eq("agent1"), eq("shared"),
                eq("input"), anyInt(), anyInt(), anyInt(), isNull(), eq("skip"));
        // skip bypasses the cross-field invariant; agent config is not read.
        verify(configValidationHelper, never())
                .validateMemoryModeAgainstAgent(anyString(), anyString(), anyString());
    }

    @Test
    void submitTask_withMemoryModeAbsent_defaultsToAlways() {
        TaskSubmissionRequest request = new TaskSubmissionRequest(
                null, "agent1", "input", null, null, null, null, null, null);

        UUID taskId = UUID.randomUUID();
        stubSuccessfulInsertExpectingMemoryMode(taskId, "always");

        taskService.submitTask(request);

        verify(taskRepository).insertTaskFromAgent(
                eq("default"), eq("agent1"), eq("shared"),
                eq("input"), anyInt(), anyInt(), anyInt(), isNull(), eq("always"));
        // always triggers the cross-field invariant against the selected agent.
        verify(configValidationHelper)
                .validateMemoryModeAgainstAgent("default", "agent1", "always");
    }

    @Test
    void submitTask_withMemoryModeAgentDecides_persistsAndValidatesAgainstAgent() {
        TaskSubmissionRequest request = new TaskSubmissionRequest(
                null, "agent1", "input", null, null, null, null, null, "agent_decides");

        UUID taskId = UUID.randomUUID();
        stubSuccessfulInsertExpectingMemoryMode(taskId, "agent_decides");

        taskService.submitTask(request);

        verify(taskRepository).insertTaskFromAgent(
                eq("default"), eq("agent1"), eq("shared"),
                eq("input"), anyInt(), anyInt(), anyInt(), isNull(), eq("agent_decides"));
        verify(configValidationHelper)
                .validateMemoryModeAgainstAgent("default", "agent1", "agent_decides");
    }

    @Test
    void submitTask_withMemoryModeAlwaysForMemoryDisabledAgent_throwsValidation() {
        TaskSubmissionRequest request = new TaskSubmissionRequest(
                null, "agent1", "input", null, null, null, null, null, "always");

        doThrow(new ValidationException(
                "memory_mode cannot be 'always' because this agent does not have memory enabled"))
                .when(configValidationHelper)
                .validateMemoryModeAgainstAgent("default", "agent1", "always");

        ValidationException ex = assertThrows(ValidationException.class,
                () -> taskService.submitTask(request));
        assertTrue(ex.getMessage().contains("memory_mode"));
        verify(taskRepository, never()).insertTaskFromAgent(anyString(), anyString(), anyString(),
                anyString(), anyInt(), anyInt(), anyInt(), any(), anyString());
    }

    @Test
    void submitTask_emptyAttachedMemoryIdsMirrorAsEmptyArrayInEventDetails() {
        TaskSubmissionRequest request = new TaskSubmissionRequest(
                null, "agent1", "input", null, null, null, null, null, null);

        UUID taskId = UUID.randomUUID();
        stubSuccessfulInsert(taskId);

        taskService.submitTask(request);

        ArgumentCaptor<String> detailsCaptor = ArgumentCaptor.forClass(String.class);
        verify(taskEventService).recordEvent(
                eq("default"), eq(taskId), eq("agent1"),
                eq("task_submitted"), isNull(), eq("queued"),
                isNull(), isNull(), isNull(), detailsCaptor.capture());
        String details = detailsCaptor.getValue();
        assertTrue(details.contains("attached_memory_ids"),
                "Event details must include attached_memory_ids key even when empty; got: " + details);
        assertTrue(details.contains("[]"),
                "Empty list should render as []; got: " + details);
    }

    // Helper to stub a successful task insert without caring about memory_mode value.
    private void stubSuccessfulInsert(UUID taskId) {
        Timestamp now = Timestamp.from(Instant.now());
        Map<String, Object> inserted = new LinkedHashMap<>();
        inserted.put("task_id", taskId);
        inserted.put("agent_display_name_snapshot", "Agent One");
        inserted.put("created_at", now);
        when(taskRepository.insertTaskFromAgent(
                anyString(), anyString(), anyString(),
                anyString(), anyInt(), anyInt(), anyInt(), any(), anyString()))
                .thenReturn(Optional.of(inserted));
    }

    private void stubSuccessfulInsertExpectingMemoryMode(UUID taskId, String expectedMode) {
        Timestamp now = Timestamp.from(Instant.now());
        Map<String, Object> inserted = new LinkedHashMap<>();
        inserted.put("task_id", taskId);
        inserted.put("agent_display_name_snapshot", "Agent One");
        inserted.put("created_at", now);
        when(taskRepository.insertTaskFromAgent(
                anyString(), anyString(), anyString(),
                anyString(), anyInt(), anyInt(), anyInt(), any(), eq(expectedMode)))
                .thenReturn(Optional.of(inserted));
    }

    // --- getTaskStatus tests ---

    @Test
    void getTaskStatus_existingTask_returnsStatus() {
        UUID taskId = UUID.randomUUID();
        Timestamp now = Timestamp.from(Instant.now());
        Map<String, Object> taskRow = new LinkedHashMap<>();
        taskRow.put("task_id", taskId);
        taskRow.put("agent_id", "agent1");
        taskRow.put("agent_display_name_snapshot", "Agent One");
        taskRow.put("status", "queued");
        taskRow.put("input", "test input");
        taskRow.put("output", null);
        taskRow.put("retry_count", 0);
        taskRow.put("retry_history", "[]");
        taskRow.put("lease_owner", null);
        taskRow.put("last_error_code", null);
        taskRow.put("last_error_message", null);
        taskRow.put("last_worker_id", null);
        taskRow.put("dead_letter_reason", null);
        taskRow.put("dead_lettered_at", null);
        taskRow.put("created_at", now);
        taskRow.put("updated_at", now);
        taskRow.put("checkpoint_count", 3L);
        taskRow.put("total_cost_microdollars", 999L);
        taskRow.put("langfuse_endpoint_id", null);
        taskRow.put("pending_input_prompt", null);
        taskRow.put("pending_approval_action", null);
        taskRow.put("human_input_timeout_at", null);
        taskRow.put("pause_reason", null);
        taskRow.put("pause_details", null);
        taskRow.put("resume_eligible_at", null);
        taskRow.put("memory_mode", "agent_decides");

        when(taskRepository.findByIdWithAggregates(taskId, "default")).thenReturn(Optional.of(taskRow));
        when(taskObservabilityService.getTaskCostTotals(taskId, "default"))
                .thenReturn(new CheckpointCostTotals(5000L, 120, 40, 160, 2300L));

        TaskStatusResponse response = taskService.getTaskStatus(taskId);

        assertEquals(taskId, response.taskId());
        assertEquals("agent1", response.agentId());
        assertEquals("Agent One", response.agentDisplayName());
        assertEquals("queued", response.status());
        assertEquals(3, response.checkpointCount());
        assertEquals(5000L, response.totalCostMicrodollars());
        assertNull(response.pendingInputPrompt());
        assertNull(response.pendingApprovalAction());
        assertNull(response.humanInputTimeoutAt());
        assertNull(response.pauseReason());
        assertNull(response.pauseDetails());
        assertNull(response.resumeEligibleAt());
        assertEquals("agent_decides", response.memoryMode());
    }

    @Test
    void getTaskObservability_existingTask_returnsCheckpointBasedResponse() {
        UUID taskId = UUID.randomUUID();
        Timestamp now = Timestamp.from(Instant.now());
        Map<String, Object> taskRow = new LinkedHashMap<>();
        taskRow.put("task_id", taskId);
        taskRow.put("agent_id", "agent1");
        taskRow.put("agent_display_name_snapshot", "Agent One");
        taskRow.put("status", "dead_letter");
        taskRow.put("input", "test input");
        taskRow.put("output", "{\"result\":\"done\"}");
        taskRow.put("retry_count", 1);
        taskRow.put("retry_history", "[\"2026-03-27T17:00:03Z\"]");
        taskRow.put("lease_owner", null);
        taskRow.put("last_error_code", "retryable_error");
        taskRow.put("last_error_message", "network down");
        taskRow.put("last_worker_id", "worker-1");
        taskRow.put("dead_letter_reason", "retries_exhausted");
        taskRow.put("dead_lettered_at", Timestamp.from(Instant.parse("2026-03-27T17:00:06Z")));
        taskRow.put("created_at", now);
        taskRow.put("updated_at", now);
        taskRow.put("checkpoint_count", 2L);
        taskRow.put("langfuse_endpoint_id", null);

        when(taskRepository.findByIdWithAggregates(taskId, "default")).thenReturn(Optional.of(taskRow));
        Map<String, Object> cp1 = checkpointRow("cp-1", "input", "worker-1", "2026-03-27T17:00:01Z");
        Map<String, Object> cp2 = checkpointRow("cp-2", "loop", "worker-1", "2026-03-27T17:00:04Z");
        cp2.put("cost_microdollars", 5200);
        cp2.put("execution_metadata", "{\"input_tokens\":120,\"output_tokens\":40,\"model\":\"test-model\"}");
        when(taskRepository.getCheckpoints(taskId, "default")).thenReturn(Optional.of(List.of(cp1, cp2)));

        TaskObservabilityResponse response = taskService.getTaskObservability(taskId);

        assertTrue(response.enabled());
        assertEquals(taskId, response.taskId());
        assertEquals("agent1", response.agentId());
        assertEquals("Agent One", response.agentDisplayName());
        assertEquals("dead_letter", response.status());
        assertEquals(5200L, response.totalCostMicrodollars());
        assertEquals(120, response.inputTokens());
        assertEquals(40, response.outputTokens());
        assertEquals(160, response.totalTokens());
        assertEquals(3000L, response.durationMs());
        // Expect: 2 checkpoint items + 1 resumed_after_retry + 1 dead_lettered = 4
        assertEquals(4, response.items().size());
        assertEquals("checkpoint_persisted", response.items().get(0).kind());
        assertEquals("resumed_after_retry", response.items().get(1).kind());
        assertEquals("checkpoint_persisted", response.items().get(2).kind());
        assertEquals("dead_lettered", response.items().get(3).kind());
    }

    private Map<String, Object> checkpointRow(String checkpointId, String nodeName, String workerId, String createdAtIso) {
        Map<String, Object> row = new LinkedHashMap<>();
        row.put("checkpoint_id", checkpointId);
        row.put("metadata_payload", "{\"writes\":{\"%s\":{}}}".formatted(nodeName));
        row.put("checkpoint_payload", "{}");
        row.put("worker_id", workerId);
        row.put("created_at", Timestamp.from(Instant.parse(createdAtIso)));
        row.put("cost_microdollars", 0);
        row.put("execution_metadata", null);
        return row;
    }

    @Test
    void getTaskStatus_notFound_throwsException() {
        UUID taskId = UUID.randomUUID();
        when(taskRepository.findByIdWithAggregates(taskId, "default")).thenReturn(Optional.empty());

        assertThrows(TaskNotFoundException.class, () -> taskService.getTaskStatus(taskId));
    }

    // --- cancelTask tests ---

    @Test
    void cancelTask_queuedTask_succeeds() {
        UUID taskId = UUID.randomUUID();
        when(taskRepository.cancelTask(taskId, "default"))
                .thenReturn(new CancelResult(MutationResult.UPDATED, "queued", "agent1"));

        TaskCancelResponse response = taskService.cancelTask(taskId);

        assertEquals(taskId, response.taskId());
        assertEquals("dead_letter", response.status());
        assertEquals("cancelled_by_user", response.deadLetterReason());
        verify(taskEventService).recordEvent(eq("default"), eq(taskId), eq("agent1"),
                eq("task_cancelled"), eq("queued"), eq("dead_letter"),
                isNull(), eq("cancelled_by_user"), isNull(), eq("{}"));
    }

    @Test
    void cancelTask_completedTask_throwsConflict() {
        UUID taskId = UUID.randomUUID();
        when(taskRepository.cancelTask(taskId, "default"))
                .thenReturn(new CancelResult(MutationResult.WRONG_STATE, "completed", "agent1"));

        assertThrows(InvalidStateTransitionException.class, () -> taskService.cancelTask(taskId));
    }

    @Test
    void cancelTask_notFound_throwsNotFound() {
        UUID taskId = UUID.randomUUID();
        when(taskRepository.cancelTask(taskId, "default"))
                .thenReturn(new CancelResult(MutationResult.NOT_FOUND, null, null));

        assertThrows(TaskNotFoundException.class, () -> taskService.cancelTask(taskId));
    }

    // --- redriveTask tests ---

    @Test
    void redriveTask_deadLetteredTask_succeeds() {
        UUID taskId = UUID.randomUUID();
        when(taskRepository.redriveTask(taskId, "default"))
                .thenReturn(new RedriveResult(MutationResult.UPDATED, "agent1"));

        RedriveResponse response = taskService.redriveTask(taskId);

        assertEquals(taskId, response.taskId());
        assertEquals("queued", response.status());
        verify(taskEventService).recordEvent(eq("default"), eq(taskId), eq("agent1"),
                eq("task_redriven"), eq("dead_letter"), eq("queued"),
                isNull(), isNull(), isNull(), eq("{}"));
    }

    @Test
    void redriveTask_queuedTask_throwsConflict() {
        UUID taskId = UUID.randomUUID();
        when(taskRepository.redriveTask(taskId, "default"))
                .thenReturn(new RedriveResult(MutationResult.WRONG_STATE, null));

        assertThrows(InvalidStateTransitionException.class, () -> taskService.redriveTask(taskId));
    }

    @Test
    void redriveTask_notFound_throwsNotFound() {
        UUID taskId = UUID.randomUUID();
        when(taskRepository.redriveTask(taskId, "default"))
                .thenReturn(new RedriveResult(MutationResult.NOT_FOUND, null));

        assertThrows(TaskNotFoundException.class, () -> taskService.redriveTask(taskId));
    }

    // --- listDeadLetterTasks tests ---

    @Test
    void listDeadLetterTasks_withAgentFilter_returnsFilteredList() {
        Timestamp now = Timestamp.from(Instant.now());
        UUID taskId = UUID.randomUUID();
        Map<String, Object> row = new LinkedHashMap<>();
        row.put("task_id", taskId);
        row.put("agent_id", "agent1");
        row.put("agent_display_name_snapshot", "Agent One");
        row.put("dead_letter_reason", "non_retryable_error");
        row.put("last_error_code", "tool_args_invalid");
        row.put("last_error_message", "validation failed");
        row.put("retry_count", 1);
        row.put("last_worker_id", "worker-1");
        row.put("dead_lettered_at", now);
        when(taskRepository.listDeadLetterTasks("default", "agent1", 50)).thenReturn(List.of(row));

        DeadLetterListResponse response = taskService.listDeadLetterTasks("agent1", null);

        assertEquals(1, response.items().size());
        assertEquals(taskId, response.items().get(0).taskId());
    }

    @Test
    void listDeadLetterTasks_limitCapped() {
        when(taskRepository.listDeadLetterTasks("default", null, 200)).thenReturn(List.of());

        taskService.listDeadLetterTasks(null, 500); // should cap at 200

        verify(taskRepository).listDeadLetterTasks("default", null, 200);
    }

    // --- getCheckpoints tests ---

    @Test
    void getCheckpoints_existingTask_returnsCheckpoints() {
        UUID taskId = UUID.randomUUID();
        Timestamp now = Timestamp.from(Instant.now());
        List<Map<String, Object>> rows = List.of(
                Map.of(
                        "checkpoint_id", "cp-1",
                        "worker_id", "worker-a",
                        "cost_microdollars", 5200,
                        "execution_metadata", "{\"latency_ms\": 2340}",
                        "metadata_payload", "{\"source\": \"agent\"}",
                        "checkpoint_payload", """
                                {"channel_values":{"messages":[{"kwargs":{"type":"human","content":"what is 2+2?"}}]}}
                                """,
                        "created_at", now));
        when(taskRepository.getCheckpoints(taskId, "default")).thenReturn(Optional.of(rows));

        CheckpointListResponse response = taskService.getCheckpoints(taskId);

        assertEquals(1, response.checkpoints().size());
        assertEquals("cp-1", response.checkpoints().get(0).checkpointId());
        assertEquals(1, response.checkpoints().get(0).stepNumber());
        assertEquals("agent", response.checkpoints().get(0).nodeName());
        assertEquals("input", response.checkpoints().get(0).event().type());
        assertEquals("what is 2+2?", response.checkpoints().get(0).event().summary());
    }

    @Test
    void getCheckpoints_aiToolCall_returnsParsedToolEvent() {
        UUID taskId = UUID.randomUUID();
        Timestamp now = Timestamp.from(Instant.now());
        List<Map<String, Object>> rows = List.of(
                checkpointRow(
                        "cp-tool-call",
                        "worker-a",
                        0,
                        null,
                        "{\"source\": \"loop\"}",
                        """
                                {
                                  "channel_values": {
                                    "messages": [
                                      {
                                        "kwargs": {
                                          "type": "ai",
                                          "content": [{"type":"tool_use","name":"calculator","input":{"expression":"2+2"}}],
                                          "tool_calls": [{"name":"calculator","args":{"expression":"2+2"}}],
                                          "usage_metadata": {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12}
                                        }
                                      }
                                    ]
                                  }
                                }
                                """,
                        now));
        when(taskRepository.getCheckpoints(taskId, "default")).thenReturn(Optional.of(rows));

        CheckpointEventResponse event = taskService.getCheckpoints(taskId).checkpoints().get(0).event();

        assertEquals("tool_call", event.type());
        assertEquals("Tool Call: calculator", event.title());
        assertEquals("calculator", event.toolName());
        assertTrue(event.toolArgs() instanceof Map<?, ?>);
        assertTrue(event.usage() instanceof Map<?, ?>);
    }

    @Test
    void getCheckpoints_toolResult_returnsParsedToolResultEvent() {
        UUID taskId = UUID.randomUUID();
        Timestamp now = Timestamp.from(Instant.now());
        List<Map<String, Object>> rows = List.of(
                checkpointRow(
                        "cp-tool-result",
                        "worker-a",
                        0,
                        null,
                        "{\"source\": \"loop\"}",
                        """
                                {
                                  "channel_values": {
                                    "messages": [
                                      {
                                        "kwargs": {
                                          "type": "tool",
                                          "name": "calculator",
                                          "content": "{\\"expression\\": \\"2+2\\", \\"result\\": 4}"
                                        }
                                      }
                                    ]
                                  }
                                }
                                """,
                        now));
        when(taskRepository.getCheckpoints(taskId, "default")).thenReturn(Optional.of(rows));

        CheckpointEventResponse event = taskService.getCheckpoints(taskId).checkpoints().get(0).event();

        assertEquals("tool_result", event.type());
        assertEquals("Tool Result: calculator", event.title());
        assertTrue(event.toolResult() instanceof Map<?, ?>);
    }

    @Test
    void getCheckpoints_multipleToolCalls_returnsAggregatedToolEvent() {
        UUID taskId = UUID.randomUUID();
        Timestamp now = Timestamp.from(Instant.now());
        List<Map<String, Object>> rows = List.of(
                checkpointRow(
                        "cp-multi-tool",
                        "worker-a",
                        0,
                        null,
                        "{\"source\": \"loop\"}",
                        """
                                {
                                  "channel_values": {
                                    "messages": [
                                      {
                                        "kwargs": {
                                          "type": "ai",
                                          "tool_calls": [
                                            {"name":"calculator","args":{"expression":"2+2"}},
                                            {"name":"read_url","args":{"url":"https://example.com"}}
                                          ]
                                        }
                                      }
                                    ]
                                  }
                                }
                                """,
                        now));
        when(taskRepository.getCheckpoints(taskId, "default")).thenReturn(Optional.of(rows));

        CheckpointEventResponse event = taskService.getCheckpoints(taskId).checkpoints().get(0).event();

        assertEquals("tool_call", event.type());
        assertEquals("Tool Calls", event.title());
        assertNull(event.toolName());
        assertTrue(event.toolArgs() instanceof List<?>);
        assertTrue(event.summary().contains("calculator"));
        assertTrue(event.summary().contains("read_url"));
    }

    @Test
    void getCheckpoints_memoryWriteCheckpoint_returnsMemorySavedEvent() {
        UUID taskId = UUID.randomUUID();
        Timestamp now = Timestamp.from(Instant.now());
        // The memory_write node writes pending_memory but does NOT append to
        // messages. Without the dedicated branch in CheckpointEventParser, the
        // parser would re-render the trailing "ai" message as a duplicate
        // "Agent Response" step. source is always "loop" (LangGraph's standard
        // superstep label), so detection must key off channel_values.
        List<Map<String, Object>> rows = List.of(
                checkpointRow(
                        "cp-memory-write",
                        "worker-a",
                        0,
                        null,
                        "{\"source\": \"loop\", \"step\": 6}",
                        """
                                {
                                  "channel_values": {
                                    "messages": [
                                      {"kwargs": {"type":"ai","content":"final answer"}}
                                    ],
                                    "pending_memory": {
                                      "title": "Completed: test task",
                                      "summary": "Agent produced final answer.",
                                      "outcome": "succeeded",
                                      "content_vec": [0.1, 0.2, 0.3],
                                      "summarizer_model_id": "claude-haiku-4-5",
                                      "observations_snapshot": ["obs 1", "obs 2"],
                                      "tags": [],
                                      "summarizer_tokens_in": 500,
                                      "summarizer_tokens_out": 80,
                                      "summarizer_cost_microdollars": 1200,
                                      "embedding_tokens": 42,
                                      "embedding_cost_microdollars": 5
                                    }
                                  }
                                }
                                """,
                        now));
        when(taskRepository.getCheckpoints(taskId, "default")).thenReturn(Optional.of(rows));

        CheckpointEventResponse event = taskService.getCheckpoints(taskId).checkpoints().get(0).event();

        assertEquals("system", event.type());
        assertEquals("Memory Saved", event.title());
        // The memory_write step is rendered as prose (title + summary). None
        // of the internal fields — observations, embedding vector, or
        // accounting — leak into the task timeline.
        assertNull(event.content());
        assertEquals(
                "Completed: test task\n\nAgent produced final answer.",
                event.summary());
    }

    @Test
    void getCheckpoints_multipleMemoryWrites_relabelsSubsequentAsUpdate() {
        // First memory_write creates the agent_memory_entries row; subsequent
        // memory_write steps (from follow-ups or redrives) UPSERT the same row
        // via ON CONFLICT(task_id), so rendering them all as "Memory Saved"
        // would imply multiple memories exist when only one does.
        UUID taskId = UUID.randomUUID();
        Timestamp now = Timestamp.from(Instant.now());
        String memoryPayload = """
                {
                  "channel_values": {
                    "messages": [ {"kwargs": {"type":"ai","content":"done"}} ],
                    "pending_memory": {
                      "title": "First pass",
                      "summary": "Initial memory write.",
                      "outcome": "succeeded"
                    }
                  }
                }
                """;
        String memoryPayload2 = """
                {
                  "channel_values": {
                    "messages": [ {"kwargs": {"type":"ai","content":"done again"}} ],
                    "pending_memory": {
                      "title": "Refreshed",
                      "summary": "Follow-up refreshed memory.",
                      "outcome": "succeeded"
                    }
                  }
                }
                """;
        List<Map<String, Object>> rows = List.of(
                checkpointRow("cp-memory-1", "worker-a", 0, null,
                        "{\"source\": \"loop\", \"step\": 6}", memoryPayload, now),
                checkpointRow("cp-memory-2", "worker-a", 0, null,
                        "{\"source\": \"loop\", \"step\": 12}", memoryPayload2,
                        Timestamp.from(now.toInstant().plusSeconds(10))));
        when(taskRepository.getCheckpoints(taskId, "default")).thenReturn(Optional.of(rows));

        List<CheckpointResponse> result = taskService.getCheckpoints(taskId).checkpoints();

        assertEquals(2, result.size());
        assertEquals("Memory Saved", result.get(0).event().title());
        assertEquals("Memory Updated", result.get(1).event().title());
        // Summary on the updated event still reflects the fresh memory content.
        assertTrue(result.get(1).event().summary().contains("Follow-up refreshed memory."));
    }

    @Test
    void getCheckpoints_malformedPayload_fallsBackToCheckpointEvent() {
        UUID taskId = UUID.randomUUID();
        Timestamp now = Timestamp.from(Instant.now());
        List<Map<String, Object>> rows = List.of(
                checkpointRow(
                        "cp-bad-json",
                        "worker-a",
                        0,
                        null,
                        "{\"source\": \"loop\", \"step\": 3}",
                        "{not valid json",
                        now));
        when(taskRepository.getCheckpoints(taskId, "default")).thenReturn(Optional.of(rows));

        CheckpointResponse checkpoint = taskService.getCheckpoints(taskId).checkpoints().get(0);

        assertEquals("loop", checkpoint.nodeName());
        assertEquals("checkpoint", checkpoint.event().type());
        assertTrue(checkpoint.event().summary().contains("Framework step \"loop\" completed"));
    }

    @Test
    void getCheckpoints_notFound_throwsException() {
        UUID taskId = UUID.randomUUID();
        when(taskRepository.getCheckpoints(taskId, "default")).thenReturn(Optional.empty());

        assertThrows(TaskNotFoundException.class, () -> taskService.getCheckpoints(taskId));
    }

    // --- listTasks tests ---

    @Test
    void listTasks_invalidStatus_throwsValidation() {
        assertThrows(ValidationException.class, () -> taskService.listTasks("garbage", null, null, null));
    }

    @Test
    void listTasks_usesCheapFallbackCostWithoutObservabilityFanout() {
        Timestamp now = Timestamp.from(Instant.now());
        UUID taskId = UUID.randomUUID();
        Map<String, Object> row = new LinkedHashMap<>();
        row.put("task_id", taskId);
        row.put("agent_id", "agent1");
        row.put("agent_display_name_snapshot", "Agent One");
        row.put("status", "completed");
        row.put("retry_count", 0);
        row.put("checkpoint_count", 2L);
        row.put("total_cost_microdollars", 0L);
        row.put("created_at", now);
        row.put("updated_at", now);
        row.put("pause_reason", null);
        row.put("resume_eligible_at", null);

        when(taskRepository.listTasks("default", null, null, null, 50)).thenReturn(List.of(row));

        TaskListResponse response = taskService.listTasks(null, null, null, null);

        assertEquals(1, response.items().size());
        assertEquals(0L, response.items().get(0).totalCostMicrodollars());
        verifyNoInteractions(taskObservabilityService);
    }

    // --- getHealth tests ---

    @Test
    void getHealth_dbConnected_returnsHealthy() {
        when(taskRepository.isDatabaseConnected()).thenReturn(true);
        when(taskRepository.getActiveWorkerCount()).thenReturn(3);
        when(taskRepository.getQueuedTaskCount()).thenReturn(12);

        HealthResponse response = taskService.getHealth();

        assertEquals("healthy", response.status());
        assertEquals("connected", response.database());
        assertEquals(3, response.activeWorkers());
        assertEquals(12, response.queuedTasks());
    }

    @Test
    void getHealth_dbDisconnected_returnsUnhealthy() {
        when(taskRepository.isDatabaseConnected()).thenReturn(false);

        HealthResponse response = taskService.getHealth();

        assertEquals("unhealthy", response.status());
        assertEquals("disconnected", response.database());
        assertEquals(0, response.activeWorkers());
        assertEquals(0, response.queuedTasks());
    }

    private Map<String, Object> checkpointRow(
            String checkpointId,
            String workerId,
            int costMicrodollars,
            Object executionMetadata,
            String metadataPayload,
            String checkpointPayload,
            Timestamp createdAt) {
        Map<String, Object> row = new LinkedHashMap<>();
        row.put("checkpoint_id", checkpointId);
        row.put("worker_id", workerId);
        row.put("cost_microdollars", costMicrodollars);
        row.put("execution_metadata", executionMetadata);
        row.put("metadata_payload", metadataPayload);
        row.put("checkpoint_payload", checkpointPayload);
        row.put("created_at", createdAt);
        return row;
    }
}
