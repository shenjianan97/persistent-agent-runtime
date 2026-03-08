package com.persistentagent.api.exception;

import java.util.UUID;

public class InvalidStateTransitionException extends RuntimeException {

    private final UUID taskId;

    public InvalidStateTransitionException(UUID taskId, String message) {
        super(message);
        this.taskId = taskId;
    }

    public UUID getTaskId() {
        return taskId;
    }
}
