package com.persistentagent.api.model.response;

import com.fasterxml.jackson.annotation.JsonInclude;
import com.fasterxml.jackson.annotation.JsonProperty;

import java.time.OffsetDateTime;
import java.util.List;
import java.util.UUID;

/**
 * Planning Primitive P3 — response DTO for {@code GET /v1/tasks/{taskId}/plan}.
 *
 * <p>Projects the {@code plan} channel from the latest root checkpoint. Items
 * are returned in stored order (worker-written insertion order). The
 * {@code updated_at} field reflects the {@code created_at} of the checkpoint
 * the plan was projected from; it is {@code null} when no checkpoint exists yet.
 *
 * <p>An empty {@code plan} list is always a 200 — it means the agent has not
 * called {@code plan_write} yet (or the plan channel is absent in the latest
 * checkpoint). Only a missing task row is a 404.
 */
@JsonInclude(JsonInclude.Include.NON_NULL)
public record TaskPlanResponse(
        @JsonProperty("task_id") UUID taskId,
        @JsonProperty("plan") List<PlanItem> plan,
        @JsonProperty("updated_at") OffsetDateTime updatedAt
) {

    /**
     * A single plan item as written by the worker's {@code plan_write} tool.
     * Mirrors {@code RuntimeState.plan[*]}: {@code id} is unique across items,
     * {@code status} is one of {@code pending}, {@code in_progress}, or {@code completed}.
     */
    public record PlanItem(
            @JsonProperty("id") String id,
            @JsonProperty("title") String title,
            @JsonProperty("status") String status
    ) {}
}
