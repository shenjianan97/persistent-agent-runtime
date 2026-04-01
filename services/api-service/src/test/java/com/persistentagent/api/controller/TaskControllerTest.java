package com.persistentagent.api.controller;

import com.persistentagent.api.exception.AgentNotFoundException;
import com.persistentagent.api.exception.InvalidStateTransitionException;
import com.persistentagent.api.exception.TaskNotFoundException;
import com.persistentagent.api.exception.ValidationException;
import com.persistentagent.api.model.response.*;
import com.persistentagent.api.service.TaskEventService;
import com.persistentagent.api.service.TaskService;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.autoconfigure.web.servlet.WebMvcTest;
import org.springframework.test.context.bean.override.mockito.MockitoBean;
import org.springframework.http.MediaType;
import org.springframework.test.web.servlet.MockMvc;

import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.util.List;
import java.util.UUID;

import static org.mockito.ArgumentMatchers.*;
import static org.mockito.Mockito.when;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.*;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.*;

@WebMvcTest(TaskController.class)
class TaskControllerTest {

        @Autowired
        private MockMvc mockMvc;

        @MockitoBean
        private TaskService taskService;

        @MockitoBean
        private TaskEventService taskEventService;

        // --- POST /v1/tasks ---

        @Test
        void submitTask_validRequest_returns201() throws Exception {
                UUID taskId = UUID.randomUUID();
                OffsetDateTime now = OffsetDateTime.now(ZoneOffset.UTC);
                TaskSubmissionResponse response = new TaskSubmissionResponse(taskId, "agent1", "Agent One", "queued", now);
                when(taskService.submitTask(any())).thenReturn(response);

                String body = """
                                {
                                  "agent_id": "agent1",
                                  "input": "test input"
                                }
                                """;

                mockMvc.perform(post("/v1/tasks")
                                .contentType(MediaType.APPLICATION_JSON_VALUE)
                                .content(body))
                                .andExpect(status().isCreated())
                                .andExpect(jsonPath("$.task_id").value(taskId.toString()))
                                .andExpect(jsonPath("$.agent_id").value("agent1"))
                                .andExpect(jsonPath("$.agent_display_name").value("Agent One"))
                                .andExpect(jsonPath("$.status").value("queued"));
        }

        @Test
        void submitTask_withLangfuseEndpointId_returns201() throws Exception {
                UUID taskId = UUID.randomUUID();
                UUID endpointId = UUID.randomUUID();
                OffsetDateTime now = OffsetDateTime.now(ZoneOffset.UTC);
                TaskSubmissionResponse response = new TaskSubmissionResponse(taskId, "agent1", "Agent One", "queued", now);
                when(taskService.submitTask(any())).thenReturn(response);

                String body = """
                                {
                                  "agent_id": "agent1",
                                  "input": "test input",
                                  "langfuse_endpoint_id": "%s"
                                }
                                """.formatted(endpointId);

                mockMvc.perform(post("/v1/tasks")
                                .contentType(MediaType.APPLICATION_JSON_VALUE)
                                .content(body))
                                .andExpect(status().isCreated())
                                .andExpect(jsonPath("$.task_id").value(taskId.toString()))
                                .andExpect(jsonPath("$.status").value("queued"));
        }

        @Test
        void submitTask_missingAgentId_returns400() throws Exception {
                String body = """
                                {
                                  "input": "test"
                                }
                                """;

                mockMvc.perform(post("/v1/tasks")
                                .contentType(MediaType.APPLICATION_JSON_VALUE)
                                .content(body))
                                .andExpect(status().isBadRequest());
        }

        @Test
        void submitTask_missingInput_returns400() throws Exception {
                String body = """
                                {
                                  "agent_id": "agent1"
                                }
                                """;

                mockMvc.perform(post("/v1/tasks")
                                .contentType(MediaType.APPLICATION_JSON_VALUE)
                                .content(body))
                                .andExpect(status().isBadRequest());
        }

        @Test
        void submitTask_agentNotFound_returns404() throws Exception {
                when(taskService.submitTask(any()))
                                .thenThrow(new AgentNotFoundException("agent-unknown"));

                String body = """
                                {
                                  "agent_id": "agent-unknown",
                                  "input": "test"
                                }
                                """;

                mockMvc.perform(post("/v1/tasks")
                                .contentType(MediaType.APPLICATION_JSON_VALUE)
                                .content(body))
                                .andExpect(status().isNotFound())
                                .andExpect(jsonPath("$.message").exists());
        }

        @Test
        void submitTask_disabledAgent_returns400() throws Exception {
                when(taskService.submitTask(any()))
                                .thenThrow(new ValidationException("Agent is disabled and cannot be used for task submission: agent-disabled"));

                String body = """
                                {
                                  "agent_id": "agent-disabled",
                                  "input": "test"
                                }
                                """;

                mockMvc.perform(post("/v1/tasks")
                                .contentType(MediaType.APPLICATION_JSON_VALUE)
                                .content(body))
                                .andExpect(status().isBadRequest())
                                .andExpect(jsonPath("$.message").exists());
        }

        @Test
        void submitTask_modelDeactivated_returns400() throws Exception {
                when(taskService.submitTask(any()))
                                .thenThrow(new ValidationException("Agent's model is no longer active. Update the agent's model before submitting tasks: agent1"));

                String body = """
                                {
                                  "agent_id": "agent1",
                                  "input": "test"
                                }
                                """;

                mockMvc.perform(post("/v1/tasks")
                                .contentType(MediaType.APPLICATION_JSON_VALUE)
                                .content(body))
                                .andExpect(status().isBadRequest())
                                .andExpect(jsonPath("$.message").exists());
        }

        @Test
        void submitTask_maxRetriesOutOfRange_returns400() throws Exception {
                String body = """
                                {
                                  "agent_id": "agent1",
                                  "input": "test",
                                  "max_retries": 15
                                }
                                """;

                mockMvc.perform(post("/v1/tasks")
                                .contentType(MediaType.APPLICATION_JSON_VALUE)
                                .content(body))
                                .andExpect(status().isBadRequest());
        }

        @Test
        void submitTask_maxStepsOutOfRange_returns400() throws Exception {
                String body = """
                                {
                                  "agent_id": "agent1",
                                  "input": "test",
                                  "max_steps": 0
                                }
                                """;

                mockMvc.perform(post("/v1/tasks")
                                .contentType(MediaType.APPLICATION_JSON_VALUE)
                                .content(body))
                                .andExpect(status().isBadRequest());
        }

        @Test
        void submitTask_taskTimeoutOutOfRange_returns400() throws Exception {
                String body = """
                                {
                                  "agent_id": "agent1",
                                  "input": "test",
                                  "task_timeout_seconds": 0
                                }
                                """;

                mockMvc.perform(post("/v1/tasks")
                                .contentType(MediaType.APPLICATION_JSON_VALUE)
                                .content(body))
                                .andExpect(status().isBadRequest());
        }

        // --- GET /v1/tasks/{taskId} ---

        @Test
        void getTaskStatus_existingTask_returns200() throws Exception {
                UUID taskId = UUID.randomUUID();
                OffsetDateTime now = OffsetDateTime.now(ZoneOffset.UTC);
                TaskStatusResponse response = new TaskStatusResponse(
                                taskId, "agent1", "Agent One", "running", "test input", null,
                                0, List.of(), 5, 12500L, "worker-abc-123",
                                null, null, null, null, null, now, now, null);
                when(taskService.getTaskStatus(taskId)).thenReturn(response);

                mockMvc.perform(get("/v1/tasks/" + taskId))
                                .andExpect(status().isOk())
                                .andExpect(jsonPath("$.task_id").value(taskId.toString()))
                                .andExpect(jsonPath("$.checkpoint_count").value(5))
                                .andExpect(jsonPath("$.total_cost_microdollars").value(12500));
        }

        @Test
        void getTaskStatus_notFound_returns404() throws Exception {
                UUID taskId = UUID.randomUUID();
                when(taskService.getTaskStatus(taskId)).thenThrow(new TaskNotFoundException(taskId));

                mockMvc.perform(get("/v1/tasks/" + taskId))
                                .andExpect(status().isNotFound());
        }

        @Test
        void getTaskStatus_invalidUuid_returns400() throws Exception {
                mockMvc.perform(get("/v1/tasks/not-a-uuid"))
                                .andExpect(status().isBadRequest());
        }

        // --- GET /v1/tasks/{taskId}/checkpoints ---

        @Test
        void getCheckpoints_existingTask_returns200() throws Exception {
                UUID taskId = UUID.randomUUID();
                OffsetDateTime now = OffsetDateTime.now(ZoneOffset.UTC);
                CheckpointListResponse response = new CheckpointListResponse(List.of(
                                new CheckpointResponse(
                                                "cp-1",
                                                1,
                                                "agent",
                                                "worker-a",
                                                5200,
                                                null,
                                                new CheckpointEventResponse("input", "User Input", "what is 2+2?", "what is 2+2?", null, null, null, null),
                                                now)));
                when(taskService.getCheckpoints(taskId)).thenReturn(response);

                mockMvc.perform(get("/v1/tasks/" + taskId + "/checkpoints"))
                                .andExpect(status().isOk())
                                .andExpect(jsonPath("$.checkpoints[0].checkpoint_id").value("cp-1"))
                                .andExpect(jsonPath("$.checkpoints[0].step_number").value(1))
                                .andExpect(jsonPath("$.checkpoints[0].node_name").value("agent"))
                                .andExpect(jsonPath("$.checkpoints[0].event.type").value("input"));
        }

        @Test
        void getTaskObservability_existingTask_returns200() throws Exception {
                UUID taskId = UUID.randomUUID();
                TaskObservabilityItemResponse item = new TaskObservabilityItemResponse(
                                "checkpoint-cp-1",
                                null,
                                "checkpoint_persisted",
                                "Checkpoint saved",
                                "Saved durable progress at step 1.",
                                1,
                                "agent",
                                null,
                                null,
                                5200L,
                                120,
                                40,
                                160,
                                2300L,
                                null,
                                null,
                                OffsetDateTime.parse("2026-03-27T17:00:00Z"),
                                null);
                TaskObservabilityResponse response = new TaskObservabilityResponse(
                                true,
                                taskId,
                                "agent1",
                                "Agent One",
                                "completed",
                                5200L,
                                120,
                                40,
                                160,
                                2300L,
                                List.of(item));
                when(taskService.getTaskObservability(taskId)).thenReturn(response);

                mockMvc.perform(get("/v1/tasks/" + taskId + "/observability"))
                                .andExpect(status().isOk())
                                .andExpect(jsonPath("$.enabled").value(true))
                                .andExpect(jsonPath("$.task_id").value(taskId.toString()))
                                .andExpect(jsonPath("$.total_cost_microdollars").value(5200))
                                .andExpect(jsonPath("$.items[0].item_id").value("checkpoint-cp-1"))
                                .andExpect(jsonPath("$.items[0].kind").value("checkpoint_persisted"));
        }

        // --- POST /v1/tasks/{taskId}/cancel ---

        @Test
        void cancelTask_success_returns200() throws Exception {
                UUID taskId = UUID.randomUUID();
                TaskCancelResponse response = new TaskCancelResponse(taskId, "dead_letter", "cancelled_by_user");
                when(taskService.cancelTask(taskId)).thenReturn(response);

                mockMvc.perform(post("/v1/tasks/" + taskId + "/cancel"))
                                .andExpect(status().isOk())
                                .andExpect(jsonPath("$.status").value("dead_letter"))
                                .andExpect(jsonPath("$.dead_letter_reason").value("cancelled_by_user"));
        }

        @Test
        void cancelTask_invalidState_returns409() throws Exception {
                UUID taskId = UUID.randomUUID();
                when(taskService.cancelTask(taskId))
                                .thenThrow(new InvalidStateTransitionException(taskId, "Cannot cancel"));

                mockMvc.perform(post("/v1/tasks/" + taskId + "/cancel"))
                                .andExpect(status().isConflict());
        }

        // --- GET /v1/tasks/dead-letter ---

        @Test
        void listDeadLetterTasks_returns200() throws Exception {
                UUID taskId = UUID.randomUUID();
                OffsetDateTime now = OffsetDateTime.now(ZoneOffset.UTC);
                DeadLetterListResponse response = new DeadLetterListResponse(List.of(
                                new DeadLetterItemResponse(taskId, "agent1", "Agent One", "non_retryable_error",
                                                "tool_args_invalid", "validation failed", 1, "worker-1", now)));
                when(taskService.listDeadLetterTasks(eq("agent1"), eq(50))).thenReturn(response);

                mockMvc.perform(get("/v1/tasks/dead-letter")
                                .param("agent_id", "agent1")
                                .param("limit", "50"))
                                .andExpect(status().isOk())
                                .andExpect(jsonPath("$.items[0].task_id").value(taskId.toString()));
        }

        // --- POST /v1/tasks/{taskId}/redrive ---

        @Test
        void redriveTask_success_returns200() throws Exception {
                UUID taskId = UUID.randomUUID();
                RedriveResponse response = new RedriveResponse(taskId, "queued");
                when(taskService.redriveTask(taskId)).thenReturn(response);

                mockMvc.perform(post("/v1/tasks/" + taskId + "/redrive"))
                                .andExpect(status().isOk())
                                .andExpect(jsonPath("$.task_id").value(taskId.toString()))
                                .andExpect(jsonPath("$.status").value("queued"));
        }

        @Test
        void redriveTask_invalidState_returns409() throws Exception {
                UUID taskId = UUID.randomUUID();
                when(taskService.redriveTask(taskId))
                                .thenThrow(new InvalidStateTransitionException(taskId, "Not dead_letter"));

                mockMvc.perform(post("/v1/tasks/" + taskId + "/redrive"))
                                .andExpect(status().isConflict());
        }
}
