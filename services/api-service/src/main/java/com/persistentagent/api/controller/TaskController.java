package com.persistentagent.api.controller;

import com.persistentagent.api.config.ValidationConstants;
import com.persistentagent.api.model.request.TaskRejectRequest;
import com.persistentagent.api.model.request.TaskRespondRequest;
import com.persistentagent.api.model.request.TaskSubmissionRequest;
import com.persistentagent.api.model.response.*;
import com.persistentagent.api.service.TaskEventService;
import com.persistentagent.api.service.TaskService;
import jakarta.validation.Valid;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.*;

import java.util.UUID;

@RestController
@RequestMapping("/v1/tasks")
public class TaskController {

    private final TaskService taskService;
    private final TaskEventService taskEventService;

    public TaskController(TaskService taskService, TaskEventService taskEventService) {
        this.taskService = taskService;
        this.taskEventService = taskEventService;
    }

    @PostMapping
    public ResponseEntity<TaskSubmissionResponse> submitTask(
            @Valid @RequestBody TaskSubmissionRequest request) {
        TaskSubmissionResponse response = taskService.submitTask(request);
        return ResponseEntity.status(HttpStatus.CREATED).body(response);
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
}
