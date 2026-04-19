package com.persistentagent.api.controller;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.persistentagent.api.config.ValidationConstants;
import com.persistentagent.api.model.request.TaskRejectRequest;
import com.persistentagent.api.model.request.TaskRespondRequest;
import com.persistentagent.api.model.request.TaskSubmissionRequest;
import com.persistentagent.api.model.response.*;
import com.persistentagent.api.service.ConversationLogService;
import com.persistentagent.api.service.TaskEventService;
import com.persistentagent.api.service.TaskService;
import jakarta.validation.ConstraintViolation;
import jakarta.validation.Valid;
import jakarta.validation.Validator;
import org.springframework.http.HttpStatus;
import org.springframework.http.MediaType;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.*;
import org.springframework.web.multipart.MultipartFile;

import java.util.List;
import java.util.Set;
import java.util.UUID;
import java.util.stream.Collectors;

@RestController
@RequestMapping("/v1/tasks")
public class TaskController {

    private final TaskService taskService;
    private final TaskEventService taskEventService;
    private final ConversationLogService conversationLogService;
    private final ObjectMapper objectMapper;
    private final Validator validator;

    public TaskController(TaskService taskService, TaskEventService taskEventService,
                          ConversationLogService conversationLogService,
                          ObjectMapper objectMapper, Validator validator) {
        this.taskService = taskService;
        this.taskEventService = taskEventService;
        this.conversationLogService = conversationLogService;
        this.objectMapper = objectMapper;
        this.validator = validator;
    }

    @PostMapping(consumes = MediaType.APPLICATION_JSON_VALUE)
    public ResponseEntity<TaskSubmissionResponse> submitTask(
            @Valid @RequestBody TaskSubmissionRequest request) {
        TaskSubmissionResponse response = taskService.submitTask(request);
        return ResponseEntity.status(HttpStatus.CREATED).body(response);
    }

    @PostMapping(consumes = MediaType.MULTIPART_FORM_DATA_VALUE)
    public ResponseEntity<TaskSubmissionResponse> submitTaskMultipart(
            @RequestPart("task_request") String taskRequestJson,
            @RequestPart(value = "files", required = false) List<MultipartFile> files) {
        try {
            TaskSubmissionRequest request = objectMapper.readValue(taskRequestJson, TaskSubmissionRequest.class);

            // Programmatically validate — the JSON endpoint gets this for free via @Valid,
            // but manual deserialization skips it. This ensures identical validation behavior.
            Set<ConstraintViolation<TaskSubmissionRequest>> violations = validator.validate(request);
            if (!violations.isEmpty()) {
                String errors = violations.stream()
                        .map(v -> v.getPropertyPath() + ": " + v.getMessage())
                        .collect(Collectors.joining(", "));
                throw new com.persistentagent.api.exception.ValidationException(errors);
            }

            TaskSubmissionResponse response = taskService.submitTaskWithFiles(request, files);
            return ResponseEntity.status(HttpStatus.CREATED).body(response);
        } catch (com.fasterxml.jackson.core.JsonProcessingException e) {
            throw new com.persistentagent.api.exception.ValidationException(
                    "Invalid task_request JSON: " + e.getMessage());
        }
    }

    @GetMapping
    public ResponseEntity<TaskListResponse> listTasks(
            @RequestParam(name = "status", required = false) String status,
            @RequestParam(name = "agent_id", required = false) String agentId,
            @RequestParam(name = "pause_reason", required = false) String pauseReason,
            @RequestParam(name = "limit", required = false) Integer limit) {
        TaskListResponse response = taskService.listTasks(status, agentId, pauseReason, limit);
        return ResponseEntity.ok(response);
    }

    @GetMapping("/{taskId}")
    public ResponseEntity<TaskStatusResponse> getTaskStatus(@PathVariable UUID taskId) {
        TaskStatusResponse response = taskService.getTaskStatus(taskId);
        return ResponseEntity.ok(response);
    }

    @GetMapping("/{taskId}/checkpoints")
    public ResponseEntity<CheckpointListResponse> getCheckpoints(@PathVariable UUID taskId) {
        CheckpointListResponse response = taskService.getCheckpoints(taskId);
        return ResponseEntity.ok(response);
    }

    @GetMapping("/{taskId}/observability")
    public ResponseEntity<TaskObservabilityResponse> getTaskObservability(@PathVariable UUID taskId) {
        TaskObservabilityResponse response = taskService.getTaskObservability(taskId);
        return ResponseEntity.ok(response);
    }

    @PostMapping("/{taskId}/cancel")
    public ResponseEntity<TaskCancelResponse> cancelTask(@PathVariable UUID taskId) {
        TaskCancelResponse response = taskService.cancelTask(taskId);
        return ResponseEntity.ok(response);
    }

    @GetMapping("/dead-letter")
    public ResponseEntity<DeadLetterListResponse> listDeadLetterTasks(
            @RequestParam(name = "agent_id", required = false) String agentId,
            @RequestParam(name = "limit", required = false) Integer limit) {
        DeadLetterListResponse response = taskService.listDeadLetterTasks(agentId, limit);
        return ResponseEntity.ok(response);
    }

    @PostMapping("/{taskId}/redrive")
    public ResponseEntity<RedriveResponse> redriveTask(@PathVariable UUID taskId) {
        RedriveResponse response = taskService.redriveTask(taskId);
        return ResponseEntity.ok(response);
    }

    @PostMapping("/{taskId}/approve")
    public ResponseEntity<RedriveResponse> approveTask(@PathVariable UUID taskId) {
        RedriveResponse response = taskService.approveTask(taskId);
        return ResponseEntity.ok(response);
    }

    @PostMapping("/{taskId}/reject")
    public ResponseEntity<RedriveResponse> rejectTask(
            @PathVariable UUID taskId,
            @Valid @RequestBody TaskRejectRequest request) {
        RedriveResponse response = taskService.rejectTask(taskId, request.reason());
        return ResponseEntity.ok(response);
    }

    @PostMapping("/{taskId}/respond")
    public ResponseEntity<RedriveResponse> respondToTask(
            @PathVariable UUID taskId,
            @Valid @RequestBody TaskRespondRequest request) {
        RedriveResponse response = taskService.respondToTask(taskId, request.message());
        return ResponseEntity.ok(response);
    }

    @PostMapping("/{taskId}/follow-up")
    public ResponseEntity<RedriveResponse> followUpTask(
            @PathVariable UUID taskId,
            @Valid @RequestBody TaskRespondRequest request) {
        RedriveResponse response = taskService.followUpTask(taskId, request.message());
        return ResponseEntity.ok(response);
    }

    @PostMapping("/{taskId}/resume")
    public ResponseEntity<RedriveResponse> resumeTask(@PathVariable UUID taskId) {
        RedriveResponse response = taskService.resumeTask(taskId);
        return ResponseEntity.ok(response);
    }

    @GetMapping("/{taskId}/events")
    public ResponseEntity<TaskEventListResponse> getTaskEvents(
            @PathVariable UUID taskId,
            @RequestParam(defaultValue = "100") int limit) {
        // Verify the task exists (throws TaskNotFoundException → 404)
        taskService.getTaskStatus(taskId);
        TaskEventListResponse events = taskEventService.listEvents(
                taskId, ValidationConstants.DEFAULT_TENANT_ID, limit);
        return ResponseEntity.ok(events);
    }

    /**
     * Phase 2 Track 7 Task 13 — user-facing conversation log for a task.
     *
     * <p>Returns the append-only {@code task_conversation_log} entries
     * ordered by monotone {@code sequence}. Pagination is exclusive on
     * {@code after_sequence}; {@code next_sequence} in the response is the
     * max sequence of the page when full, else null.
     *
     * <p>404 is returned when the task doesn't exist OR belongs to another
     * tenant — indistinguishable by design (no enumeration oracle).
     */
    @GetMapping("/{taskId}/conversation")
    public ResponseEntity<ConversationEntryResponse.Page> getTaskConversation(
            @PathVariable UUID taskId,
            @RequestParam(name = "after_sequence", required = false) Long afterSequence,
            @RequestParam(name = "limit", required = false) Integer limit) {
        ConversationEntryResponse.Page page =
                conversationLogService.getConversation(taskId, afterSequence, limit);
        return ResponseEntity.ok(page);
    }
}
