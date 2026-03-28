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
import com.persistentagent.api.repository.ModelRepository;
import com.persistentagent.api.repository.TaskRepository;
import com.persistentagent.api.repository.TaskRepository.MutationResult;
import com.persistentagent.api.service.observability.TaskObservabilityService;
import com.persistentagent.api.service.observability.TaskObservabilityTotals;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
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
    private TaskRepository taskRepository;

    @Mock
    private ModelRepository modelRepository;

    @Mock
    private TaskObservabilityService taskObservabilityService;

    private TaskService taskService;
    private ObjectMapper objectMapper;

    @BeforeEach
    void setUp() {
        objectMapper = new ObjectMapper();
        objectMapper.registerModule(new JavaTimeModule());
        objectMapper.disable(SerializationFeature.WRITE_DATES_AS_TIMESTAMPS);
        taskService = new TaskService(
                taskRepository,
                modelRepository,
                taskObservabilityService,
                objectMapper,
                new CheckpointEventParser(objectMapper),
                false
        );
    }

    // --- submitTask tests ---

    @Test
    void submitTask_validRequest_returnsCreated() {
        AgentConfigRequest config = new AgentConfigRequest(
                "You are a helper", "anthropic", "claude-sonnet-4-6", 0.7, List.of("web_search"));
        TaskSubmissionRequest request = new TaskSubmissionRequest(
                null, "agent1", config, "do something", 3, 100, 3600);

        UUID taskId = UUID.randomUUID();
        Timestamp now = Timestamp.from(Instant.now());
        Map<String, Object> inserted = Map.of("task_id", taskId, "created_at", now);
        when(modelRepository.isModelActive(anyString(), anyString())).thenReturn(true);
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
                "prompt", "anthropic", "unsupported-model", 0.5, List.of());
        TaskSubmissionRequest request = new TaskSubmissionRequest(
                null, "agent1", config, "input", null, null, null);

        assertThrows(ValidationException.class, () -> taskService.submitTask(request));
    }

    @Test
    void submitTask_unsupportedTool_throwsValidation() {
        AgentConfigRequest config = new AgentConfigRequest(
                "prompt", "anthropic", "claude-sonnet-4-6", 0.5, List.of("web_search", "hack_tool"));
        TaskSubmissionRequest request = new TaskSubmissionRequest(
                null, "agent1", config, "input", null, null, null);

        when(modelRepository.isModelActive(anyString(), anyString())).thenReturn(true);

        assertThrows(ValidationException.class, () -> taskService.submitTask(request));
    }

    @Test
    void submitTask_devOnlyToolRejectedWhenDevTaskControlsDisabled() {
        AgentConfigRequest config = new AgentConfigRequest(
                "prompt", "anthropic", "claude-sonnet-4-6", 0.5, List.of("dev_sleep"));
        TaskSubmissionRequest request = new TaskSubmissionRequest(
                null, "agent1", config, "input", null, null, null);

        when(modelRepository.isModelActive(anyString(), anyString())).thenReturn(true);

        assertThrows(ValidationException.class, () -> taskService.submitTask(request));
    }

    @Test
    void submitTask_shortTimeoutRejectedWhenDevTaskControlsDisabled() {
        AgentConfigRequest config = new AgentConfigRequest(
                "prompt", "anthropic", "claude-sonnet-4-6", 0.5, List.of());
        TaskSubmissionRequest request = new TaskSubmissionRequest(
                null, "agent1", config, "input", null, null, 30);

        when(modelRepository.isModelActive(anyString(), anyString())).thenReturn(true);

        assertThrows(ValidationException.class, () -> taskService.submitTask(request));
    }

    @Test
    void submitTask_defaultValues_usedWhenNull() {
        AgentConfigRequest config = new AgentConfigRequest(
                "prompt", "openai", "gpt-4o", null, null);
        TaskSubmissionRequest request = new TaskSubmissionRequest(
                null, "agent1", config, "input", null, null, null);

        UUID taskId = UUID.randomUUID();
        Timestamp now = Timestamp.from(Instant.now());
        when(modelRepository.isModelActive(anyString(), anyString())).thenReturn(true);
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
        taskRow.put("updated_at", now);
        taskRow.put("checkpoint_count", 3L);
        taskRow.put("total_cost_microdollars", 999L);

        when(taskRepository.findByIdWithAggregates(taskId, "default")).thenReturn(Optional.of(taskRow));
        when(taskObservabilityService.getTaskTotals(taskId, "agent1", "queued"))
                .thenReturn(new TaskObservabilityTotals(5000L, 120, 40, 160, 2300L, null));

        TaskStatusResponse response = taskService.getTaskStatus(taskId);

        assertEquals(taskId, response.taskId());
        assertEquals("agent1", response.agentId());
        assertEquals("queued", response.status());
        assertEquals(3, response.checkpointCount());
        assertEquals(5000L, response.totalCostMicrodollars());
    }

    @Test
    void getTaskObservability_existingTask_returnsNormalizedResponse() {
        UUID taskId = UUID.randomUUID();
        Timestamp now = Timestamp.from(Instant.now());
        Map<String, Object> taskRow = new LinkedHashMap<>();
        taskRow.put("task_id", taskId);
        taskRow.put("agent_id", "agent1");
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

        TaskObservabilitySpanResponse span = new TaskObservabilitySpanResponse(
                "obs-1",
                null,
                "task-id",
                "agent1",
                null,
                "llm",
                "agent",
                "claude-sonnet-4-6",
                null,
                5200L,
                120,
                40,
                160,
                2300L,
                "prompt",
                "response",
                OffsetDateTime.parse("2026-03-27T17:00:02Z"),
                OffsetDateTime.parse("2026-03-27T17:00:02.300Z")
        );
        TaskObservabilityItemResponse spanItem = new TaskObservabilityItemResponse(
                "obs-1",
                null,
                "tool_span",
                "Tool: calculator",
                "calculator completed successfully",
                2,
                "loop",
                "calculator",
                null,
                5200L,
                120,
                40,
                160,
                2300L,
                "prompt",
                "response",
                OffsetDateTime.parse("2026-03-27T17:00:02Z"),
                OffsetDateTime.parse("2026-03-27T17:00:02.300Z")
        );
        TaskObservabilityResponse observability = new TaskObservabilityResponse(
                true,
                taskId,
                "agent1",
                "dead_letter",
                "trace-1",
                5200L,
                120,
                40,
                160,
                2300L,
                List.of(span),
                List.of(spanItem)
        );

        when(taskRepository.findByIdWithAggregates(taskId, "default")).thenReturn(Optional.of(taskRow));
        when(taskRepository.getCheckpoints(taskId, "default")).thenReturn(Optional.of(List.of(
                checkpointRow("cp-1", "input", "worker-1", "2026-03-27T17:00:01Z"),
                checkpointRow("cp-2", "loop", "worker-1", "2026-03-27T17:00:04Z")
        )));
        when(taskObservabilityService.getTaskObservability(taskId, "agent1", "dead_letter"))
                .thenReturn(observability);

        TaskObservabilityResponse response = taskService.getTaskObservability(taskId);

        assertTrue(response.enabled());
        assertEquals(taskId, response.taskId());
        assertEquals("agent1", response.agentId());
        assertEquals("trace-1", response.traceId());
        assertEquals(1, response.spans().size());
        assertEquals("obs-1", response.spans().get(0).spanId());
        assertEquals("llm", response.spans().get(0).type());
        assertEquals(5, response.items().size());
        assertEquals("checkpoint_persisted", response.items().get(0).kind());
        assertEquals("tool_span", response.items().get(1).kind());
        assertEquals("resumed_after_retry", response.items().get(2).kind());
        assertEquals("checkpoint_persisted", response.items().get(3).kind());
        assertEquals("dead_lettered", response.items().get(4).kind());
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
        when(taskRepository.cancelTask(taskId, "default")).thenReturn(MutationResult.UPDATED);

        TaskCancelResponse response = taskService.cancelTask(taskId);

        assertEquals(taskId, response.taskId());
        assertEquals("dead_letter", response.status());
        assertEquals("cancelled_by_user", response.deadLetterReason());
    }

    @Test
    void cancelTask_completedTask_throwsConflict() {
        UUID taskId = UUID.randomUUID();
        when(taskRepository.cancelTask(taskId, "default")).thenReturn(MutationResult.WRONG_STATE);

        assertThrows(InvalidStateTransitionException.class, () -> taskService.cancelTask(taskId));
    }

    @Test
    void cancelTask_notFound_throwsNotFound() {
        UUID taskId = UUID.randomUUID();
        when(taskRepository.cancelTask(taskId, "default")).thenReturn(MutationResult.NOT_FOUND);

        assertThrows(TaskNotFoundException.class, () -> taskService.cancelTask(taskId));
    }

    // --- redriveTask tests ---

    @Test
    void redriveTask_deadLetteredTask_succeeds() {
        UUID taskId = UUID.randomUUID();
        when(taskRepository.redriveTask(taskId, "default")).thenReturn(MutationResult.UPDATED);

        RedriveResponse response = taskService.redriveTask(taskId);

        assertEquals(taskId, response.taskId());
        assertEquals("queued", response.status());
    }

    @Test
    void redriveTask_queuedTask_throwsConflict() {
        UUID taskId = UUID.randomUUID();
        when(taskRepository.redriveTask(taskId, "default")).thenReturn(MutationResult.WRONG_STATE);

        assertThrows(InvalidStateTransitionException.class, () -> taskService.redriveTask(taskId));
    }

    @Test
    void redriveTask_notFound_throwsNotFound() {
        UUID taskId = UUID.randomUUID();
        when(taskRepository.redriveTask(taskId, "default")).thenReturn(MutationResult.NOT_FOUND);

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
        assertThrows(ValidationException.class, () -> taskService.listTasks("garbage", null, null));
    }

    @Test
    void listTasks_enrichesCostsFromObservability() {
        Timestamp now = Timestamp.from(Instant.now());
        UUID taskId = UUID.randomUUID();
        List<Map<String, Object>> rows = List.of(Map.of(
                "task_id", taskId,
                "agent_id", "agent1",
                "status", "completed",
                "retry_count", 0,
                "checkpoint_count", 2L,
                "total_cost_microdollars", 999L,
                "created_at", now,
                "updated_at", now
        ));

        when(taskRepository.listTasks("default", null, null, 50)).thenReturn(rows);
        when(taskObservabilityService.getTaskTotals(taskId, "agent1", "completed"))
                .thenReturn(new TaskObservabilityTotals(7500L, 100, 20, 120, 1500L, "trace-1"));

        TaskListResponse response = taskService.listTasks(null, null, null);

        assertEquals(1, response.items().size());
        assertEquals(7500L, response.items().get(0).totalCostMicrodollars());
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
