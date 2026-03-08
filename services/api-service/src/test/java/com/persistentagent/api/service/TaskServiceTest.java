package com.persistentagent.api.service;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.SerializationFeature;
import com.fasterxml.jackson.datatype.jsr310.JavaTimeModule;
import com.persistentagent.api.exception.InvalidStateTransitionException;
import com.persistentagent.api.exception.TaskNotFoundException;
import com.persistentagent.api.exception.ValidationException;
import com.persistentagent.api.model.request.AgentConfigRequest;
import com.persistentagent.api.model.request.TaskSubmissionRequest;
import com.persistentagent.api.model.response.*;
import com.persistentagent.api.repository.TaskRepository;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;

import java.sql.Timestamp;
import java.time.Instant;
import java.util.*;

import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.ArgumentMatchers.*;
import static org.mockito.Mockito.*;

@ExtendWith(MockitoExtension.class)
class TaskServiceTest {

    @Mock
    private TaskRepository taskRepository;

    private TaskService taskService;
    private ObjectMapper objectMapper;

    @BeforeEach
    void setUp() {
        objectMapper = new ObjectMapper();
        objectMapper.registerModule(new JavaTimeModule());
        objectMapper.disable(SerializationFeature.WRITE_DATES_AS_TIMESTAMPS);
        taskService = new TaskService(taskRepository, objectMapper);
    }

    // --- submitTask tests ---

    @Test
    void submitTask_validRequest_returnsCreated() {
        AgentConfigRequest config = new AgentConfigRequest(
                "You are a helper", "claude-sonnet-4-6", 0.7, List.of("web_search"));
        TaskSubmissionRequest request = new TaskSubmissionRequest(
                null, "agent1", config, "do something", 3, 100, 3600);

        UUID taskId = UUID.randomUUID();
        Timestamp now = Timestamp.from(Instant.now());
        Map<String, Object> inserted = Map.of("task_id", taskId, "created_at", now);
        when(taskRepository.insertTask(anyString(), anyString(), anyString(), anyString(),
                anyString(), anyInt(), anyInt(), anyInt())).thenReturn(inserted);

        TaskSubmissionResponse response = taskService.submitTask(request);

        assertNotNull(response);
        assertEquals(taskId, response.taskId());
        assertEquals("agent1", response.agentId());
        assertEquals("queued", response.status());
        assertNotNull(response.createdAt());
    }

    @Test
    void submitTask_unsupportedModel_throwsValidation() {
        AgentConfigRequest config = new AgentConfigRequest(
                "prompt", "unsupported-model", 0.5, List.of());
        TaskSubmissionRequest request = new TaskSubmissionRequest(
                null, "agent1", config, "input", null, null, null);

        assertThrows(ValidationException.class, () -> taskService.submitTask(request));
    }

    @Test
    void submitTask_unsupportedTool_throwsValidation() {
        AgentConfigRequest config = new AgentConfigRequest(
                "prompt", "claude-sonnet-4-6", 0.5, List.of("web_search", "hack_tool"));
        TaskSubmissionRequest request = new TaskSubmissionRequest(
                null, "agent1", config, "input", null, null, null);

        assertThrows(ValidationException.class, () -> taskService.submitTask(request));
    }

    @Test
    void submitTask_defaultValues_usedWhenNull() {
        AgentConfigRequest config = new AgentConfigRequest(
                "prompt", "gpt-4o", null, null);
        TaskSubmissionRequest request = new TaskSubmissionRequest(
                null, "agent1", config, "input", null, null, null);

        UUID taskId = UUID.randomUUID();
        Timestamp now = Timestamp.from(Instant.now());
        when(taskRepository.insertTask(eq("default"), eq("agent1"), anyString(), eq("shared"),
                eq("input"), eq(3), eq(100), eq(3600)))
                .thenReturn(Map.of("task_id", taskId, "created_at", now));

        taskService.submitTask(request);

        verify(taskRepository).insertTask(eq("default"), eq("agent1"), anyString(), eq("shared"),
                eq("input"), eq(3), eq(100), eq(3600));
    }

    // --- getTaskStatus tests ---

    @Test
    void getTaskStatus_existingTask_returnsStatus() {
        UUID taskId = UUID.randomUUID();
        Timestamp now = Timestamp.from(Instant.now());
        Map<String, Object> taskRow = new LinkedHashMap<>();
        taskRow.put("task_id", taskId);
        taskRow.put("agent_id", "agent1");
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
        taskRow.put("checkpoint_count", 3L);
        taskRow.put("total_cost_microdollars", 5000L);

        when(taskRepository.findByIdWithAggregates(taskId, "default")).thenReturn(Optional.of(taskRow));

        TaskStatusResponse response = taskService.getTaskStatus(taskId);

        assertEquals(taskId, response.taskId());
        assertEquals("agent1", response.agentId());
        assertEquals("queued", response.status());
        assertEquals(3, response.checkpointCount());
        assertEquals(5000L, response.totalCostMicrodollars());
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
        Map<String, Object> taskRow = Map.of("task_id", taskId, "status", "queued");
        when(taskRepository.findByIdAndTenant(taskId, "default")).thenReturn(Optional.of(taskRow));
        when(taskRepository.cancelTask(taskId, "default")).thenReturn(1);

        TaskCancelResponse response = taskService.cancelTask(taskId);

        assertEquals(taskId, response.taskId());
        assertEquals("dead_letter", response.status());
        assertEquals("cancelled_by_user", response.deadLetterReason());
    }

    @Test
    void cancelTask_completedTask_throwsConflict() {
        UUID taskId = UUID.randomUUID();
        Map<String, Object> taskRow = Map.of("task_id", taskId, "status", "completed");
        when(taskRepository.findByIdAndTenant(taskId, "default")).thenReturn(Optional.of(taskRow));
        when(taskRepository.cancelTask(taskId, "default")).thenReturn(0);

        assertThrows(InvalidStateTransitionException.class, () -> taskService.cancelTask(taskId));
    }

    @Test
    void cancelTask_notFound_throwsNotFound() {
        UUID taskId = UUID.randomUUID();
        when(taskRepository.findByIdAndTenant(taskId, "default")).thenReturn(Optional.empty());

        assertThrows(TaskNotFoundException.class, () -> taskService.cancelTask(taskId));
    }

    // --- redriveTask tests ---

    @Test
    void redriveTask_deadLetteredTask_succeeds() {
        UUID taskId = UUID.randomUUID();
        Map<String, Object> taskRow = Map.of("task_id", taskId, "status", "dead_letter");
        when(taskRepository.findByIdAndTenant(taskId, "default")).thenReturn(Optional.of(taskRow));
        when(taskRepository.redriveTask(taskId, "default")).thenReturn(Optional.of(taskId));

        RedriveResponse response = taskService.redriveTask(taskId);

        assertEquals(taskId, response.taskId());
        assertEquals("queued", response.status());
    }

    @Test
    void redriveTask_queuedTask_throwsConflict() {
        UUID taskId = UUID.randomUUID();
        Map<String, Object> taskRow = Map.of("task_id", taskId, "status", "queued");
        when(taskRepository.findByIdAndTenant(taskId, "default")).thenReturn(Optional.of(taskRow));
        when(taskRepository.redriveTask(taskId, "default")).thenReturn(Optional.empty());

        assertThrows(InvalidStateTransitionException.class, () -> taskService.redriveTask(taskId));
    }

    @Test
    void redriveTask_notFound_throwsNotFound() {
        UUID taskId = UUID.randomUUID();
        when(taskRepository.findByIdAndTenant(taskId, "default")).thenReturn(Optional.empty());

        assertThrows(TaskNotFoundException.class, () -> taskService.redriveTask(taskId));
    }

    // --- listDeadLetterTasks tests ---

    @Test
    void listDeadLetterTasks_withAgentFilter_returnsFilteredList() {
        Timestamp now = Timestamp.from(Instant.now());
        UUID taskId = UUID.randomUUID();
        List<Map<String, Object>> rows = List.of(Map.of(
                "task_id", taskId,
                "agent_id", "agent1",
                "dead_letter_reason", "non_retryable_error",
                "last_error_code", "tool_args_invalid",
                "last_error_message", "validation failed",
                "retry_count", 1,
                "last_worker_id", "worker-1",
                "dead_lettered_at", now));
        when(taskRepository.listDeadLetterTasks("default", "agent1", 50)).thenReturn(rows);

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
                        "created_at", now));
        when(taskRepository.getCheckpoints(taskId, "default")).thenReturn(rows);

        CheckpointListResponse response = taskService.getCheckpoints(taskId);

        assertEquals(1, response.checkpoints().size());
        assertEquals("cp-1", response.checkpoints().get(0).checkpointId());
        assertEquals(1, response.checkpoints().get(0).stepNumber());
        assertEquals("agent", response.checkpoints().get(0).nodeName());
    }

    @Test
    void getCheckpoints_notFound_throwsException() {
        UUID taskId = UUID.randomUUID();
        when(taskRepository.getCheckpoints(taskId, "default")).thenReturn(null);

        assertThrows(TaskNotFoundException.class, () -> taskService.getCheckpoints(taskId));
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
}
