package com.persistentagent.api.model.response;

import com.fasterxml.jackson.annotation.JsonInclude;
import com.fasterxml.jackson.annotation.JsonProperty;
import com.persistentagent.api.model.ArtifactMetadata;

import java.time.OffsetDateTime;
import java.util.List;
import java.util.UUID;

public record TaskStatusResponse(
        @JsonProperty("task_id") UUID taskId,
        @JsonProperty("agent_id") String agentId,
        @JsonProperty("agent_display_name") String agentDisplayName,
        String status,
        String input,
        Object output,
        @JsonProperty("retry_count") int retryCount,
        @JsonProperty("retry_history") List<Object> retryHistory,
        @JsonProperty("checkpoint_count") int checkpointCount,
        @JsonProperty("total_cost_microdollars") long totalCostMicrodollars,
        @JsonProperty("lease_owner") String leaseOwner,
        @JsonProperty("last_error_code") String lastErrorCode,
        @JsonProperty("last_error_message") String lastErrorMessage,
        @JsonProperty("last_worker_id") String lastWorkerId,
        @JsonProperty("dead_letter_reason") String deadLetterReason,
        @JsonProperty("dead_lettered_at") OffsetDateTime deadLetteredAt,
        @JsonProperty("created_at") OffsetDateTime createdAt,
        @JsonProperty("updated_at") OffsetDateTime updatedAt,
        @JsonProperty("langfuse_endpoint_id") UUID langfuseEndpointId,
        @JsonInclude(JsonInclude.Include.NON_NULL) @JsonProperty("pending_input_prompt") String pendingInputPrompt,
        @JsonInclude(JsonInclude.Include.NON_NULL) @JsonProperty("pending_approval_action") Object pendingApprovalAction,
        @JsonInclude(JsonInclude.Include.NON_NULL) @JsonProperty("human_input_timeout_at") OffsetDateTime humanInputTimeoutAt,
        @JsonInclude(JsonInclude.Include.NON_NULL) @JsonProperty("pause_reason") String pauseReason,
        @JsonInclude(JsonInclude.Include.NON_NULL) @JsonProperty("pause_details") Object pauseDetails,
        @JsonInclude(JsonInclude.Include.NON_NULL) @JsonProperty("resume_eligible_at") OffsetDateTime resumeEligibleAt,
        @JsonInclude(JsonInclude.Include.NON_NULL) @JsonProperty("artifacts") List<ArtifactMetadata> artifacts,
        /**
         * Attached memory ids resolved from {@code task_attached_memories} in
         * {@code position} order. Always present — empty list for legacy tasks
         * created before Track 5, or for new tasks with no attachments.
         */
        @JsonProperty("attached_memory_ids") List<UUID> attachedMemoryIds,
        /**
         * Live preview rows (memory_id + title) for attachments whose memory entries
         * still resolve within the task's {@code (tenant_id, agent_id)} scope.
         * Always present — may be shorter than {@code attached_memory_ids} when
         * entries have been deleted.
         */
        @JsonProperty("attached_memories_preview") List<AttachedMemoryPreview> attachedMemoriesPreview
) {
}
