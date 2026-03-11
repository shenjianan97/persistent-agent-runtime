package com.persistentagent.api.controller;

import com.persistentagent.api.model.request.DevExpireLeaseRequest;
import com.persistentagent.api.model.request.DevForceDeadLetterRequest;
import com.persistentagent.api.model.response.DevTaskMutationResponse;
import com.persistentagent.api.service.DevTaskControlService;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.*;

import java.util.UUID;

@RestController
@RequestMapping("/v1/dev/tasks")
@ConditionalOnProperty(prefix = "app.dev-task-controls", name = "enabled", havingValue = "true")
public class DevTaskControlsController {

    private final DevTaskControlService devTaskControlService;

    public DevTaskControlsController(DevTaskControlService devTaskControlService) {
        this.devTaskControlService = devTaskControlService;
    }

    @PostMapping("/{taskId}/expire-lease")
    public ResponseEntity<DevTaskMutationResponse> expireLease(
            @PathVariable UUID taskId,
            @RequestBody(required = false) DevExpireLeaseRequest request) {
        return ResponseEntity.ok(devTaskControlService.expireLease(taskId, request));
    }

    @PostMapping("/{taskId}/force-dead-letter")
    public ResponseEntity<DevTaskMutationResponse> forceDeadLetter(
            @PathVariable UUID taskId,
            @RequestBody(required = false) DevForceDeadLetterRequest request) {
        return ResponseEntity.ok(devTaskControlService.forceDeadLetter(taskId, request));
    }
}
