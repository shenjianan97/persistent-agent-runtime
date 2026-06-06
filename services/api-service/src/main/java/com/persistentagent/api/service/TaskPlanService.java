package com.persistentagent.api.service;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.persistentagent.api.config.ValidationConstants;
import com.persistentagent.api.exception.TaskNotFoundException;
import com.persistentagent.api.model.response.TaskPlanResponse;
import com.persistentagent.api.repository.TaskRepository;
import com.persistentagent.api.util.DateTimeUtil;
import com.persistentagent.api.util.JsonParseUtil;
import org.springframework.stereotype.Service;

import java.time.OffsetDateTime;
import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.UUID;

/**
 * Planning Primitive P3 — projects the {@code plan} channel from the latest
 * root-namespace checkpoint for a task.
 *
 * <p>The plan lives in
 * {@code checkpoint_payload.channel_values.plan} (same container as
 * {@code messages}) and is a list of
 * {@code {id: string, title: string, status: "pending"|"in_progress"|"completed"}}
 * dicts written by the worker's {@code plan_write} tool.
 *
 * <p>Reuses {@link TaskRepository#getLatestRootCheckpoint} and the same JSON
 * payload-access pattern as {@link ActivityProjectionService} — no new
 * checkpoint-read path.
 */
@Service
public class TaskPlanService {

    private final TaskRepository taskRepository;
    private final ObjectMapper objectMapper;

    public TaskPlanService(TaskRepository taskRepository, ObjectMapper objectMapper) {
        this.taskRepository = taskRepository;
        this.objectMapper = objectMapper;
    }

    /**
     * Returns the current plan for the given task.
     *
     * <ul>
     *   <li>404 when the task does not exist (or belongs to another tenant).</li>
     *   <li>200 {@code {plan: []}} when the task exists but has no plan channel
     *       yet (agent never called {@code plan_write}, or no checkpoint written).
     *       {@code updated_at} is {@code null} when no checkpoint exists.</li>
     *   <li>200 {@code {plan: [{...}]}} with items in stored order when a plan
     *       channel is present. {@code updated_at} reflects the
     *       {@code created_at} of the checkpoint the plan was projected from.</li>
     * </ul>
     *
     * @param taskId the task to project the plan from
     * @return the task plan response (never {@code null})
     * @throws TaskNotFoundException when the task does not exist for the default tenant
     */
    public TaskPlanResponse getPlan(UUID taskId) {
        String tenantId = ValidationConstants.DEFAULT_TENANT_ID;

        Optional<Map<String, Object>> checkpointOpt =
                taskRepository.getLatestRootCheckpoint(taskId, tenantId);

        if (checkpointOpt.isEmpty()) {
            // No checkpoint — verify the task row itself exists (tenant-scoped).
            if (taskRepository.findByIdAndTenant(taskId, tenantId).isEmpty()) {
                throw new TaskNotFoundException(taskId);
            }
            // Task exists but has no checkpoint yet → empty plan, null updated_at.
            return new TaskPlanResponse(taskId, Collections.emptyList(), null);
        }

        Map<String, Object> row = checkpointOpt.get();

        // Extract updated_at from the checkpoint row (same DateTimeUtil conversion as
        // ActivityProjectionService uses for checkpoint timestamps).
        OffsetDateTime updatedAt = DateTimeUtil.toOffsetDateTime(row.get("created_at"));

        // Parse the checkpoint payload and extract channel_values.plan.
        List<TaskPlanResponse.PlanItem> planItems = extractPlanItems(row.get("checkpoint_payload"));

        return new TaskPlanResponse(taskId, planItems, updatedAt);
    }

    // -------------------------------------------------------------------------
    // Plan extraction from checkpoint_payload.channel_values.plan
    // -------------------------------------------------------------------------

    private List<TaskPlanResponse.PlanItem> extractPlanItems(Object payload) {
        Map<String, Object> parsed = JsonParseUtil.parseJsonMap(objectMapper, payload);
        if (parsed == null) {
            return Collections.emptyList();
        }
        Object channelValues = parsed.get("channel_values");
        if (!(channelValues instanceof Map<?, ?> channelMap)) {
            return Collections.emptyList();
        }
        Object planRaw = channelMap.get("plan");
        if (!(planRaw instanceof List<?> planList)) {
            return Collections.emptyList();
        }
        List<TaskPlanResponse.PlanItem> items = new ArrayList<>(planList.size());
        for (Object entry : planList) {
            if (!(entry instanceof Map<?, ?> itemMap)) {
                continue;
            }
            String id = asString(itemMap.get("id"));
            String title = asString(itemMap.get("title"));
            String status = asString(itemMap.get("status"));
            items.add(new TaskPlanResponse.PlanItem(id, title, status));
        }
        return Collections.unmodifiableList(items);
    }

    private static String asString(Object value) {
        if (value == null) return null;
        return value instanceof String s ? s : value.toString();
    }
}
