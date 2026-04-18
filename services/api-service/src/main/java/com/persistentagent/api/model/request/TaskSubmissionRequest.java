package com.persistentagent.api.model.request;

import com.fasterxml.jackson.annotation.JsonProperty;
import jakarta.validation.constraints.Max;
import jakarta.validation.constraints.Min;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.Size;

import java.util.List;
import java.util.UUID;

public record TaskSubmissionRequest(
        @JsonProperty("tenant_id")
        String tenantId,

        @NotBlank(message = "agent_id is required")
        @Size(max = 64, message = "agent_id must not exceed 64 characters")
        @JsonProperty("agent_id")
        String agentId,

        @NotBlank(message = "input is required")
        @Size(max = 102400, message = "input must not exceed 100KB")
        String input,

        @Min(value = 0, message = "max_retries must be >= 0")
        @Max(value = 10, message = "max_retries must be <= 10")
        @JsonProperty("max_retries")
        Integer maxRetries,

        @Min(value = 1, message = "max_steps must be >= 1")
        @Max(value = 1000, message = "max_steps must be <= 1000")
        @JsonProperty("max_steps")
        Integer maxSteps,

        @Min(value = 1, message = "task_timeout_seconds must be >= 1")
        @Max(value = 86400, message = "task_timeout_seconds must be <= 86400")
        @JsonProperty("task_timeout_seconds")
        Integer taskTimeoutSeconds,

        @JsonProperty("langfuse_endpoint_id")
        UUID langfuseEndpointId,

        /**
         * Optional list of memory entry ids to attach to this task. Each id must
         * belong to the caller's (tenant_id, agent_id). Order is preserved in
         * {@code task_attached_memories.position}.
         *
         * <p>Capped at 50 entries. This cap is a plan-level guard against blowing
         * the initial prompt context; it is not codified in the design doc, which
         * specifies only a Console-side token-footprint indicator.
         */
        @JsonProperty("attached_memory_ids")
        List<UUID> attachedMemoryIds,

        /**
         * Per-task privacy override. When {@code true}, the worker treats the task
         * as if the agent's memory were disabled (no memory tools, no write node,
         * no dead-letter memory hook) — even if {@code agent.memory.enabled} is true.
         * Defaults to {@code false} when absent.
         */
        @JsonProperty("skip_memory_write")
        Boolean skipMemoryWrite
) {
}
