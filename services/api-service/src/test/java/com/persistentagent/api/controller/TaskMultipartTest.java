package com.persistentagent.api.controller;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.persistentagent.api.exception.ValidationException;
import com.persistentagent.api.model.response.TaskSubmissionResponse;
import com.persistentagent.api.service.ActivityProjectionService;
import com.persistentagent.api.service.TaskEventService;
import com.persistentagent.api.service.TaskService;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.autoconfigure.web.servlet.WebMvcTest;
import org.springframework.http.MediaType;
import org.springframework.mock.web.MockMultipartFile;
import org.springframework.test.context.bean.override.mockito.MockitoBean;
import org.springframework.test.web.servlet.MockMvc;

import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.util.UUID;

import static org.mockito.ArgumentMatchers.any;
import static org.mockito.Mockito.when;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.multipart;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

@WebMvcTest(TaskController.class)
class TaskMultipartTest {

    @Autowired
    private MockMvc mockMvc;

    @MockitoBean
    private TaskService taskService;

    @MockitoBean
    private TaskEventService taskEventService;

    @MockitoBean
    private ActivityProjectionService activityProjectionService;

    @Autowired
    private ObjectMapper objectMapper;

    @Test
    void submitMultipart_withValidRequestAndFile_returns201() throws Exception {
        UUID taskId = UUID.randomUUID();
        OffsetDateTime now = OffsetDateTime.now(ZoneOffset.UTC);
        when(taskService.submitTaskWithFiles(any(), any()))
                .thenReturn(new TaskSubmissionResponse(taskId, "agent-1", "Agent One", "queued", now, java.util.List.of(), java.util.List.of()));

        String taskJson = """
                {"agent_id": "agent-1", "input": "Process this file"}
                """;

        MockMultipartFile taskPart = new MockMultipartFile(
                "task_request", "", MediaType.APPLICATION_JSON_VALUE, taskJson.getBytes());
        MockMultipartFile filePart = new MockMultipartFile(
                "files", "document.pdf", "application/pdf", "fake pdf content".getBytes());

        mockMvc.perform(multipart("/v1/tasks")
                        .file(taskPart)
                        .file(filePart))
                .andExpect(status().isCreated())
                .andExpect(jsonPath("$.task_id").value(taskId.toString()))
                .andExpect(jsonPath("$.status").value("queued"));
    }

    @Test
    void submitMultipart_withoutFiles_returns201() throws Exception {
        UUID taskId = UUID.randomUUID();
        OffsetDateTime now = OffsetDateTime.now(ZoneOffset.UTC);
        when(taskService.submitTaskWithFiles(any(), any()))
                .thenReturn(new TaskSubmissionResponse(taskId, "agent-1", "Agent One", "queued", now, java.util.List.of(), java.util.List.of()));

        String taskJson = """
                {"agent_id": "agent-1", "input": "No files here"}
                """;

        MockMultipartFile taskPart = new MockMultipartFile(
                "task_request", "", MediaType.APPLICATION_JSON_VALUE, taskJson.getBytes());

        mockMvc.perform(multipart("/v1/tasks")
                        .file(taskPart))
                .andExpect(status().isCreated())
                .andExpect(jsonPath("$.task_id").value(taskId.toString()));
    }

    @Test
    void submitMultipart_invalidJsonInTaskRequest_returns400() throws Exception {
        MockMultipartFile taskPart = new MockMultipartFile(
                "task_request", "", MediaType.APPLICATION_JSON_VALUE,
                "not valid json {{{".getBytes());

        mockMvc.perform(multipart("/v1/tasks")
                        .file(taskPart))
                .andExpect(status().isBadRequest());
    }

    @Test
    void submitMultipart_missingAgentId_returns400() throws Exception {
        String taskJson = """
                {"input": "missing agent_id"}
                """;

        MockMultipartFile taskPart = new MockMultipartFile(
                "task_request", "", MediaType.APPLICATION_JSON_VALUE, taskJson.getBytes());

        mockMvc.perform(multipart("/v1/tasks")
                        .file(taskPart))
                .andExpect(status().isBadRequest());
    }

    @Test
    void submitMultipart_missingInput_returns400() throws Exception {
        String taskJson = """
                {"agent_id": "agent-1"}
                """;

        MockMultipartFile taskPart = new MockMultipartFile(
                "task_request", "", MediaType.APPLICATION_JSON_VALUE, taskJson.getBytes());

        mockMvc.perform(multipart("/v1/tasks")
                        .file(taskPart))
                .andExpect(status().isBadRequest());
    }

    @Test
    void submitMultipart_sandboxNotEnabled_returns400() throws Exception {
        when(taskService.submitTaskWithFiles(any(), any()))
                .thenThrow(new ValidationException(
                        "File attachments require an agent with sandbox enabled."));

        String taskJson = """
                {"agent_id": "agent-no-sandbox", "input": "Process this file"}
                """;

        MockMultipartFile taskPart = new MockMultipartFile(
                "task_request", "", MediaType.APPLICATION_JSON_VALUE, taskJson.getBytes());
        MockMultipartFile filePart = new MockMultipartFile(
                "files", "doc.txt", "text/plain", "content".getBytes());

        mockMvc.perform(multipart("/v1/tasks")
                        .file(taskPart)
                        .file(filePart))
                .andExpect(status().isBadRequest())
                .andExpect(jsonPath("$.message").exists());
    }
}
