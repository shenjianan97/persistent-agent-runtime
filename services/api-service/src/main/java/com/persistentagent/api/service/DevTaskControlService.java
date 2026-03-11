package com.persistentagent.api.service;

import com.persistentagent.api.config.ValidationConstants;
import com.persistentagent.api.exception.InvalidStateTransitionException;
import com.persistentagent.api.exception.TaskNotFoundException;
import com.persistentagent.api.exception.ValidationException;
import com.persistentagent.api.model.request.DevExpireLeaseRequest;
import com.persistentagent.api.model.request.DevForceDeadLetterRequest;
import com.persistentagent.api.model.response.DevTaskMutationResponse;
import com.persistentagent.api.repository.TaskRepository;
import org.springframework.stereotype.Service;

import java.util.UUID;

@Service
public class DevTaskControlService {

    private static final String DEFAULT_ERROR_MESSAGE = "Forced dead letter by dev control";

    private final TaskRepository taskRepository;

    public DevTaskControlService(TaskRepository taskRepository) {
        this.taskRepository = taskRepository;
    }

    public DevTaskMutationResponse expireLease(UUID taskId, DevExpireLeaseRequest request) {
        String tenantId = "default";
        taskRepository.findByIdAndTenant(taskId, tenantId)
                .orElseThrow(() -> new TaskNotFoundException(taskId));

        String leaseOwnerOverride = request != null ? request.leaseOwner() : null;
        boolean updated = taskRepository.expireLease(taskId, tenantId, leaseOwnerOverride);
        if (!updated) {
            throw new InvalidStateTransitionException(taskId,
                    "Task " + taskId + " cannot have its lease expired (must be running with an active lease)");
        }

        return new DevTaskMutationResponse(taskId, "running", "lease expired for recovery testing");
    }

    public DevTaskMutationResponse forceDeadLetter(UUID taskId, DevForceDeadLetterRequest request) {
        String tenantId = "default";
        taskRepository.findByIdAndTenant(taskId, tenantId)
                .orElseThrow(() -> new TaskNotFoundException(taskId));

        String reason = request != null && request.reason() != null && !request.reason().isBlank()
                ? request.reason()
                : ValidationConstants.DEFAULT_DEAD_LETTER_REASON;
        if (!ValidationConstants.ALLOWED_DEAD_LETTER_REASONS.contains(reason)) {
            throw new ValidationException("Unsupported dead_letter reason: " + reason);
        }

        String errorCode = request != null && request.errorCode() != null && !request.errorCode().isBlank()
                ? request.errorCode()
                : reason;
        String errorMessage = request != null && request.errorMessage() != null && !request.errorMessage().isBlank()
                ? request.errorMessage()
                : DEFAULT_ERROR_MESSAGE;
        String lastWorkerId = request != null ? request.lastWorkerId() : null;

        boolean updated = taskRepository.forceDeadLetter(taskId, tenantId, reason, errorCode, errorMessage, lastWorkerId);
        if (!updated) {
            throw new InvalidStateTransitionException(taskId,
                    "Task " + taskId + " cannot be forced to dead letter (must be queued or running)");
        }

        return new DevTaskMutationResponse(taskId, "dead_letter", "task moved to dead letter for recovery testing");
    }
}
