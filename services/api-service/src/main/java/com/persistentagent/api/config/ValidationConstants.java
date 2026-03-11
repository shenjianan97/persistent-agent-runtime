package com.persistentagent.api.config;

import java.util.Set;

/**
 * Compile-time constants for Phase 1 API validation.
 */
public final class ValidationConstants {

    private ValidationConstants() {
    }

    /** Phase 1 tenant ID - always resolved internally. */
    public static final String DEFAULT_TENANT_ID = "default";

    /** Phase 1 worker pool ID - always "shared". */
    public static final String DEFAULT_WORKER_POOL_ID = "shared";

    /** Stable public tools available in all environments. */
    public static final Set<String> ALLOWED_TOOLS = Set.of("web_search", "read_url", "calculator");

    /** Dev-only task-control tools, enabled behind app.dev-task-controls.enabled. */
    public static final Set<String> DEV_TASK_CONTROL_TOOLS = Set.of("dev_sleep");

    /** Allowed dead-letter reasons matching the database constraint. */
    public static final Set<String> ALLOWED_DEAD_LETTER_REASONS = Set.of(
            "cancelled_by_user",
            "retries_exhausted",
            "task_timeout",
            "non_retryable_error",
            "max_steps_exceeded"
    );

    public static final String DEFAULT_DEAD_LETTER_REASON = "non_retryable_error";

    /** Supported LLM models for Phase 1. */
    public static final Set<String> SUPPORTED_MODELS = Set.of(
            "claude-sonnet-4-6",
            "claude-sonnet-4-20250514",
            "claude-haiku-4-20250514",
            "gpt-4o",
            "gpt-4o-mini",
            "gpt-4-turbo",
            "us.anthropic.claude-sonnet-4-20250514-v1:0",
            "us.anthropic.claude-haiku-4-20250514-v1:0"
    );

    // Default values
    public static final int DEFAULT_MAX_RETRIES = 3;
    public static final int DEFAULT_MAX_STEPS = 100;
    public static final int DEFAULT_TASK_TIMEOUT_SECONDS = 3600;
    public static final double DEFAULT_TEMPERATURE = 0.7;

    // Dead letter listing defaults
    public static final int DEFAULT_DEAD_LETTER_LIMIT = 50;
    public static final int MAX_DEAD_LETTER_LIMIT = 200;
}
