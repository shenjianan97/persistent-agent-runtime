package com.persistentagent.api.config;

import com.fasterxml.jackson.annotation.JsonCreator;
import com.fasterxml.jackson.annotation.JsonValue;

/**
 * Canonical set of dead-letter reason values.
 *
 * <p>Each constant's {@link #getValue()} returns the snake_case string that is
 * stored in {@code tasks.dead_letter_reason} and returned by the API. Jackson
 * uses this value for both serialization ({@link JsonValue}) and
 * deserialization ({@link JsonCreator}).
 *
 * <p>Keep this enum in sync with:
 * <ul>
 *   <li>{@link ValidationConstants#ALLOWED_DEAD_LETTER_REASONS} — the runtime
 *       guard used by the dev-task-control endpoint.</li>
 *   <li>The latest {@code infrastructure/database/migrations/*_dead_letter_reason.sql}
 *       migration — the DB-level CHECK constraint is the ultimate source of truth.</li>
 * </ul>
 */
public enum DeadLetterReason {

    CANCELLED_BY_USER("cancelled_by_user"),
    RETRIES_EXHAUSTED("retries_exhausted"),
    TASK_TIMEOUT("task_timeout"),
    NON_RETRYABLE_ERROR("non_retryable_error"),
    MAX_STEPS_EXCEEDED("max_steps_exceeded"),
    HUMAN_INPUT_TIMEOUT("human_input_timeout"),
    REJECTED_BY_USER("rejected_by_user"),
    SANDBOX_LOST("sandbox_lost"),
    SANDBOX_PROVISION_FAILED("sandbox_provision_failed"),

    /**
     * Context window hard-floor safety net (Track 7).
     *
     * <p>Emitted when Tier 1 + 1.5 + 3 compaction cannot reduce estimated
     * input tokens below the model's context window. Expected to be rare in
     * practice with the 25 KB per-tool-result cap.
     *
     * <p>Migration: {@code 0015_context_exceeded_dead_letter_reason.sql}.
     */
    CONTEXT_EXCEEDED_IRRECOVERABLE("context_exceeded_irrecoverable");

    private final String value;

    DeadLetterReason(String value) {
        this.value = value;
    }

    @JsonValue
    public String getValue() {
        return value;
    }

    @JsonCreator
    public static DeadLetterReason fromValue(String value) {
        for (DeadLetterReason reason : values()) {
            if (reason.value.equals(value)) {
                return reason;
            }
        }
        throw new IllegalArgumentException("Unknown dead_letter_reason: " + value);
    }
}
