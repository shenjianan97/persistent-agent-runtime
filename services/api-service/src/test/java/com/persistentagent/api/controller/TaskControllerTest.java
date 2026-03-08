package com.persistentagent.api.controller;

import com.persistentagent.api.exception.InvalidStateTransitionException;
import com.persistentagent.api.exception.TaskNotFoundException;
import com.persistentagent.api.exception.ValidationException;
import com.persistentagent.api.model.response.*;
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

        // --- POST /v1/tasks ---

        @Test
        void submitTask_validRequest_returns201() throws Exception {
                UUID taskId = UUID.randomUUID();
                OffsetDateTime now = OffsetDateTime.now(ZoneOffset.UTC);
                TaskSubmissionResponse response = new TaskSubmissionResponse(taskId, "agent1", "queued", now);
                when(taskService.submitTask(any())).thenReturn(response);

                String body = """
                                {
                                  "agent_id": "agent1",
                                  "agent_config": {
                                    "system_prompt": "You are a helper",
                                    "model": "claude-sonnet-4-6",
                                    "temperature": 0.7,
                                    "allowed_tools": ["web_search"]
                                  },
                                  "input": "test input"
                                }
                                """;

                mockMvc.perform(post("/v1/tasks")
                                .contentType(MediaType.APPLICATION_JSON_VALUE)
                                .content(body))
                                .andExpect(status().isCreated())
                                .andExpect(jsonPath("$.task_id").value(taskId.toString()))
                                .andExpect(jsonPath("$.agent_id").value("agent1"))
                                .andExpect(jsonPath("$.status").value("queued"));
        }

        @Test
        void submitTask_missingAgentId_returns400() throws Exception {
                String body = """
                                {
                                  "agent_config": {
                                    "system_prompt": "prompt",
                                    "model": "claude-sonnet-4-6"
                                  },
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
                                  "agent_id": "agent1",
                                  "agent_config": {
                                    "system_prompt": "prompt",
                                    "model": "claude-sonnet-4-6"
                                  }
                                }
                                """;

                mockMvc.perform(post("/v1/tasks")
                                .contentType(MediaType.APPLICATION_JSON_VALUE)
                                .content(body))
                                .andExpect(status().isBadRequest());
        }

        @Test
        void submitTask_missingSystemPrompt_returns400() throws Exception {
                String body = """
                                {
                                  "agent_id": "agent1",
                                  "agent_config": {
                                    "model": "claude-sonnet-4-6"
                                  },
                                  "input": "test"
                                }
                                """;

                mockMvc.perform(post("/v1/tasks")
                                .contentType(MediaType.APPLICATION_JSON_VALUE)
                                .content(body))
                                .andExpect(status().isBadRequest());
        }

        @Test
        void submitTask_missingModel_returns400() throws Exception {
                String body = """
                                {
                                  "agent_id": "agent1",
                                  "agent_config": {
                                    "system_prompt": "prompt"
                                  },
                                  "input": "test"
                                }
                                """;

                mockMvc.perform(post("/v1/tasks")
                                .contentType(MediaType.APPLICATION_JSON_VALUE)
                                .content(body))
                                .andExpect(status().isBadRequest());
        }

        @Test
        void submitTask_unsupportedModel_returns400() throws Exception {
                when(taskService.submitTask(any()))
                                .thenThrow(new ValidationException("Unsupported model: bad-model"));

                String body = """
                                {
                                  "agent_id": "agent1",
                                  "agent_config": {
                                    "system_prompt": "prompt",
                                    "model": "bad-model"
                                  },
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
        void submitTask_unsupportedTool_returns400() throws Exception {
                when(taskService.submitTask(any()))
                                .thenThrow(new ValidationException("Unsupported tool: hack_tool"));

                String body = """
                                {
                                  "agent_id": "agent1",
                                  "agent_config": {
                                    "system_prompt": "prompt",
                                    "model": "claude-sonnet-4-6",
                                    "allowed_tools": ["web_search", "hack_tool"]
                                  },
                                  "input": "test"
                                }
                                """;

                mockMvc.perform(post("/v1/tasks")
                                .contentType(MediaType.APPLICATION_JSON_VALUE)
                                .content(body))
                                .andExpect(status().isBadRequest());
        }

        @Test
        void submitTask_temperatureTooHigh_returns400() throws Exception {
                String body = """
                                {
                                  "agent_id": "agent1",
                                  "agent_config": {
                                    "system_prompt": "prompt",
                                    "model": "claude-sonnet-4-6",
                                    "temperature": 3.0
                                  },
                                  "input": "test"
                                }
                                """;

                mockMvc.perform(post("/v1/tasks")
                                .contentType(MediaType.APPLICATION_JSON_VALUE)
                                .content(body))
                                .andExpect(status().isBadRequest());
        }

        @Test
        void submitTask_maxRetriesOutOfRange_returns400() throws Exception {
                String body = """
                                {
                                  "agent_id": "agent1",
                                  "agent_config": {
                                    "system_prompt": "prompt",
                                    "model": "claude-sonnet-4-6"
                                  },
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
                                  "agent_config": {
                                    "system_prompt": "prompt",
                                    "model": "claude-sonnet-4-6"
                                  },
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
                                  "agent_config": {
                                    "system_prompt": "prompt",
                                    "model": "claude-sonnet-4-6"
                                  },
                                  "input": "test",
                                  "task_timeout_seconds": 30
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
                                taskId, "agent1", "running", "test input", null,
                                0, List.of(), 5, 12500L, "worker-abc-123",
                                null, null, null, null, null, now, now);
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
                                new CheckpointResponse("cp-1", 1, "agent", "worker-a", 5200, null, now)));
                when(taskService.getCheckpoints(taskId)).thenReturn(response);

                mockMvc.perform(get("/v1/tasks/" + taskId + "/checkpoints"))
                                .andExpect(status().isOk())
                                .andExpect(jsonPath("$.checkpoints[0].checkpoint_id").value("cp-1"))
                                .andExpect(jsonPath("$.checkpoints[0].step_number").value(1))
                                .andExpect(jsonPath("$.checkpoints[0].node_name").value("agent"));
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
                                new DeadLetterItemResponse(taskId, "agent1", "non_retryable_error",
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
