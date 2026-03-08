package com.persistentagent.api.exception;

import java.util.UUID;

public class TaskNotFoundException extends RuntimeException {

    private final UUID taskId;

    public TaskNotFoundException(UUID taskId) {
        super("Task not found: " + taskId);
        this.taskId = taskId;
    }

    public UUID getTaskId() {
        return taskId;
    }
}
