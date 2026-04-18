package com.persistentagent.api.exception;

/**
 * Uniform not-found signal for memory surface (list, detail, search, delete).
 *
 * <p>The 404-not-403 disclosure rule means unknown ids, ids from another tenant,
 * and ids from another agent all raise this exception with the same generic
 * message — callers must not be able to distinguish the cause.
 */
public class MemoryNotFoundException extends RuntimeException {

    public static final String UNIFORM_MESSAGE = "Memory entry not found";

    public MemoryNotFoundException() {
        super(UNIFORM_MESSAGE);
    }
}
