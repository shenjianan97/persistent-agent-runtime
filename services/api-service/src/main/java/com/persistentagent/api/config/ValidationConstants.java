package com.persistentagent.api.config;

import java.util.List;
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

    /** All valid tool names accepted by the API (for validation only). */
    public static final Set<String> ALLOWED_TOOLS = Set.of(
            "web_search", "read_url", "request_human_input", "upload_artifact",
            "sandbox_exec", "sandbox_read_file", "sandbox_write_file", "sandbox_download");

    /** Platform tools auto-enabled for every agent. */
    public static final List<String> BASE_PLATFORM_TOOLS = List.of(
            "web_search", "read_url", "upload_artifact", "request_human_input");

    /** Sandbox tools auto-enabled when sandbox.enabled is true. */
    public static final List<String> SANDBOX_TOOLS = List.of(
            "sandbox_exec", "sandbox_read_file", "sandbox_write_file", "sandbox_download");

    /** Dev-only task-control tools, enabled behind app.dev-task-controls.enabled. */
    public static final Set<String> DEV_TASK_CONTROL_TOOLS = Set.of("dev_sleep");

    /** Allowed dead-letter reasons matching the database constraint. */
    public static final Set<String> ALLOWED_DEAD_LETTER_REASONS = Set.of(
            "cancelled_by_user",
            "retries_exhausted",
            "task_timeout",
            "non_retryable_error",
            "max_steps_exceeded",
            "human_input_timeout",
            "rejected_by_user",
            "sandbox_lost",
            "sandbox_provision_failed"
    );

    public static final String DEFAULT_DEAD_LETTER_REASON = "non_retryable_error";

    // Sandbox config defaults and limits
    public static final int SANDBOX_VCPU_MIN = 1;
    public static final int SANDBOX_VCPU_MAX = 8;
    public static final int SANDBOX_VCPU_DEFAULT = 2;
    public static final int SANDBOX_MEMORY_MB_MIN = 512;
    public static final int SANDBOX_MEMORY_MB_MAX = 8192;
    public static final int SANDBOX_MEMORY_MB_DEFAULT = 2048;
    public static final int SANDBOX_TIMEOUT_SECONDS_MIN = 60;
    public static final int SANDBOX_TIMEOUT_SECONDS_MAX = 86400;
    public static final int SANDBOX_TIMEOUT_SECONDS_DEFAULT = 3600;



    // Default values
    public static final int DEFAULT_MAX_RETRIES = 3;
    public static final int DEFAULT_MAX_STEPS = 100;
    public static final int DEFAULT_TASK_TIMEOUT_SECONDS = 3600;
    public static final double DEFAULT_TEMPERATURE = 0.7;

    // Task listing defaults
    public static final int DEFAULT_TASK_LIST_LIMIT = 50;
    public static final int MAX_TASK_LIST_LIMIT = 200;

    // Dead letter listing defaults
    public static final int DEFAULT_DEAD_LETTER_LIMIT = 50;
    public static final int MAX_DEAD_LETTER_LIMIT = 200;

    /** Valid task statuses matching the database CHECK constraint. */
    public static final Set<String> VALID_TASK_STATUSES = Set.of(
            "queued", "running", "completed", "dead_letter",
            "waiting_for_approval", "waiting_for_input", "paused"
    );

    // Agent listing defaults
    public static final int DEFAULT_AGENT_LIST_LIMIT = 50;
    public static final int MAX_AGENT_LIST_LIMIT = 200;

    /** Agent status constants. */
    public static final String AGENT_STATUS_ACTIVE = "active";
    public static final String AGENT_STATUS_DISABLED = "disabled";

    /** Valid agent statuses. */
    public static final Set<String> VALID_AGENT_STATUSES = Set.of(AGENT_STATUS_ACTIVE, AGENT_STATUS_DISABLED);

    // Tool server constants
    public static final String TOOL_SERVER_NAME_PATTERN = "^[a-z0-9]([a-z0-9-]*[a-z0-9])?$";
    public static final String TOOL_SERVER_STATUS_ACTIVE = "active";
    public static final String TOOL_SERVER_STATUS_DISABLED = "disabled";
    public static final Set<String> VALID_TOOL_SERVER_STATUSES = Set.of(TOOL_SERVER_STATUS_ACTIVE, TOOL_SERVER_STATUS_DISABLED);
    public static final String TOOL_SERVER_AUTH_NONE = "none";
    public static final String TOOL_SERVER_AUTH_BEARER = "bearer_token";
    public static final Set<String> VALID_TOOL_SERVER_AUTH_TYPES = Set.of(TOOL_SERVER_AUTH_NONE, TOOL_SERVER_AUTH_BEARER);
    public static final int DEFAULT_TOOL_SERVER_LIST_LIMIT = 50;
    public static final int MAX_TOOL_SERVER_LIST_LIMIT = 200;
    public static final int TOOL_SERVER_DISCOVER_TIMEOUT_MS = 10000;

}
