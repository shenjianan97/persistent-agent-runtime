package com.persistentagent.api.service;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.persistentagent.api.config.ValidationConstants;
import com.persistentagent.api.exception.InvalidStateTransitionException;
import com.persistentagent.api.exception.TaskNotFoundException;
import com.persistentagent.api.exception.ValidationException;
import com.persistentagent.api.model.request.TaskSubmissionRequest;
import com.persistentagent.api.model.response.*;
import com.persistentagent.api.repository.TaskRepository;
import org.springframework.stereotype.Service;

import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.sql.Timestamp;
import java.util.*;
import java.util.stream.IntStream;

@Service
public class TaskService {

    private final TaskRepository taskRepository;
    private final ObjectMapper objectMapper;

    public TaskService(TaskRepository taskRepository, ObjectMapper objectMapper) {
        this.taskRepository = taskRepository;
        this.objectMapper = objectMapper;
    }

    public TaskSubmissionResponse submitTask(TaskSubmissionRequest request) {
        // Additional validations beyond Bean Validation
        validateModel(request.agentConfig().model());
        validateAllowedTools(request.agentConfig().allowedTools());

        String tenantId = ValidationConstants.DEFAULT_TENANT_ID;
        String workerPoolId = ValidationConstants.DEFAULT_WORKER_POOL_ID;

        // Build agent_config_snapshot
        Map<String, Object> agentConfigSnapshot = new LinkedHashMap<>();
        agentConfigSnapshot.put("system_prompt", request.agentConfig().systemPrompt());
        agentConfigSnapshot.put("model", request.agentConfig().model());
        agentConfigSnapshot.put("temperature",
                request.agentConfig().temperature() != null
                        ? request.agentConfig().temperature()
                        : ValidationConstants.DEFAULT_TEMPERATURE);
        agentConfigSnapshot.put("allowed_tools",
                request.agentConfig().allowedTools() != null
                        ? request.agentConfig().allowedTools()
                        : List.of());

        String agentConfigJson;
        try {
            agentConfigJson = objectMapper.writeValueAsString(agentConfigSnapshot);
        } catch (JsonProcessingException e) {
            throw new ValidationException("Failed to serialize agent_config: " + e.getMessage());
        }

        int maxRetries = request.maxRetries() != null
                ? request.maxRetries()
                : ValidationConstants.DEFAULT_MAX_RETRIES;
        int maxSteps = request.maxSteps() != null
                ? request.maxSteps()
                : ValidationConstants.DEFAULT_MAX_STEPS;
        int taskTimeoutSeconds = request.taskTimeoutSeconds() != null
                ? request.taskTimeoutSeconds()
                : ValidationConstants.DEFAULT_TASK_TIMEOUT_SECONDS;

        Map<String, Object> result = taskRepository.insertTask(
                tenantId, request.agentId(), agentConfigJson, workerPoolId,
                request.input(), maxRetries, maxSteps, taskTimeoutSeconds);

        UUID taskId = (UUID) result.get("task_id");
        OffsetDateTime createdAt = toOffsetDateTime(result.get("created_at"));

        return new TaskSubmissionResponse(taskId, request.agentId(), "queued", createdAt);
    }

    public TaskStatusResponse getTaskStatus(UUID taskId) {
        String tenantId = ValidationConstants.DEFAULT_TENANT_ID;

        Map<String, Object> task = taskRepository.findByIdAndTenant(taskId, tenantId)
                .orElseThrow(() -> new TaskNotFoundException(taskId));

        Map<String, Object> aggregates = taskRepository.getCheckpointAggregates(taskId);

        int checkpointCount = ((Number) aggregates.get("checkpoint_count")).intValue();
        long totalCost = ((Number) aggregates.get("total_cost_microdollars")).longValue();

        // Parse retry_history from JSONB
        List<Object> retryHistory = parseJsonList(task.get("retry_history"));

        // Parse output from JSONB
        Object output = parseJson(task.get("output"));

        return new TaskStatusResponse(
                (UUID) task.get("task_id"),
                (String) task.get("agent_id"),
                (String) task.get("status"),
                (String) task.get("input"),
                output,
                ((Number) task.get("retry_count")).intValue(),
                retryHistory,
                checkpointCount,
                totalCost,
                (String) task.get("lease_owner"),
                (String) task.get("last_error_code"),
                (String) task.get("last_error_message"),
                (String) task.get("last_worker_id"),
                (String) task.get("dead_letter_reason"),
                toOffsetDateTime(task.get("dead_lettered_at")),
                toOffsetDateTime(task.get("created_at")),
                toOffsetDateTime(task.get("updated_at"))
        );
    }

    public CheckpointListResponse getCheckpoints(UUID taskId) {
        String tenantId = ValidationConstants.DEFAULT_TENANT_ID;

        List<Map<String, Object>> rows = taskRepository.getCheckpoints(taskId, tenantId);
        if (rows == null) {
            throw new TaskNotFoundException(taskId);
        }

        List<CheckpointResponse> checkpoints = IntStream.range(0, rows.size())
                .mapToObj(i -> {
                    Map<String, Object> row = rows.get(i);
                    String nodeName = extractNodeName(row.get("metadata_payload"));
                    Object executionMetadata = parseJson(row.get("execution_metadata"));
                    return new CheckpointResponse(
                            (String) row.get("checkpoint_id"),
                            i + 1, // step_number derived from insertion order
                            nodeName,
                            (String) row.get("worker_id"),
                            ((Number) row.get("cost_microdollars")).intValue(),
                            executionMetadata,
                            toOffsetDateTime(row.get("created_at"))
                    );
                })
                .toList();

        return new CheckpointListResponse(checkpoints);
    }

    public TaskCancelResponse cancelTask(UUID taskId) {
        String tenantId = ValidationConstants.DEFAULT_TENANT_ID;

        // First check if task exists
        taskRepository.findByIdAndTenant(taskId, tenantId)
                .orElseThrow(() -> new TaskNotFoundException(taskId));

        int rowsAffected = taskRepository.cancelTask(taskId, tenantId);
        if (rowsAffected == 0) {
            throw new InvalidStateTransitionException(taskId,
                    "Task " + taskId + " cannot be cancelled (must be in queued or running state)");
        }

        return new TaskCancelResponse(taskId, "dead_letter", "cancelled_by_user");
    }

    public DeadLetterListResponse listDeadLetterTasks(String agentId, Integer limit) {
        String tenantId = ValidationConstants.DEFAULT_TENANT_ID;

        int effectiveLimit = limit != null
                ? Math.min(Math.max(limit, 1), ValidationConstants.MAX_DEAD_LETTER_LIMIT)
                : ValidationConstants.DEFAULT_DEAD_LETTER_LIMIT;

        List<Map<String, Object>> rows = taskRepository.listDeadLetterTasks(tenantId, agentId, effectiveLimit);

        List<DeadLetterItemResponse> items = rows.stream()
                .map(row -> new DeadLetterItemResponse(
                        (UUID) row.get("task_id"),
                        (String) row.get("agent_id"),
                        (String) row.get("dead_letter_reason"),
                        (String) row.get("last_error_code"),
                        (String) row.get("last_error_message"),
                        ((Number) row.get("retry_count")).intValue(),
                        (String) row.get("last_worker_id"),
                        toOffsetDateTime(row.get("dead_lettered_at"))
                ))
                .toList();

        return new DeadLetterListResponse(items);
    }

    public RedriveResponse redriveTask(UUID taskId) {
        String tenantId = ValidationConstants.DEFAULT_TENANT_ID;

        // First check if task exists
        taskRepository.findByIdAndTenant(taskId, tenantId)
                .orElseThrow(() -> new TaskNotFoundException(taskId));

        Optional<UUID> result = taskRepository.redriveTask(taskId, tenantId);
        if (result.isEmpty()) {
            throw new InvalidStateTransitionException(taskId,
                    "Task " + taskId + " cannot be redriven (must be in dead_letter state)");
        }

        return new RedriveResponse(taskId, "queued");
    }

    public HealthResponse getHealth() {
        boolean dbConnected = taskRepository.isDatabaseConnected();
        int activeWorkers = dbConnected ? taskRepository.getActiveWorkerCount() : 0;
        int queuedTasks = dbConnected ? taskRepository.getQueuedTaskCount() : 0;

        return new HealthResponse(
                dbConnected ? "healthy" : "unhealthy",
                dbConnected ? "connected" : "disconnected",
                activeWorkers,
                queuedTasks
        );
    }

    // --- Validation helpers ---

    private void validateModel(String model) {
        if (!ValidationConstants.SUPPORTED_MODELS.contains(model)) {
            throw new ValidationException("Unsupported model: " + model
                    + ". Supported models: " + ValidationConstants.SUPPORTED_MODELS);
        }
    }

    private void validateAllowedTools(List<String> tools) {
        if (tools == null || tools.isEmpty()) {
            return; // no tools is valid
        }
        for (String tool : tools) {
            if (!ValidationConstants.ALLOWED_TOOLS.contains(tool)) {
                throw new ValidationException("Unsupported tool: " + tool
                        + ". Allowed tools: " + ValidationConstants.ALLOWED_TOOLS);
            }
        }
    }

    // --- Conversion helpers ---

    private OffsetDateTime toOffsetDateTime(Object value) {
        if (value == null) return null;
        if (value instanceof OffsetDateTime odt) return odt;
        if (value instanceof Timestamp ts) return ts.toInstant().atOffset(ZoneOffset.UTC);
        if (value instanceof java.util.Date d) return d.toInstant().atOffset(ZoneOffset.UTC);
        return null;
    }

    private List<Object> parseJsonList(Object value) {
        if (value == null) return List.of();
        try {
            if (value instanceof String s) {
                return objectMapper.readValue(s, new TypeReference<List<Object>>() {});
            }
            if (value instanceof org.postgresql.util.PGobject pgObj) {
                return objectMapper.readValue(pgObj.getValue(), new TypeReference<List<Object>>() {});
            }
            if (value instanceof List<?> list) {
                return (List<Object>) list;
            }
        } catch (Exception e) {
            // fall through
        }
        return List.of();
    }

    private Object parseJson(Object value) {
        if (value == null) return null;
        try {
            if (value instanceof String s) {
                return objectMapper.readValue(s, Object.class);
            }
            if (value instanceof org.postgresql.util.PGobject pgObj) {
                String val = pgObj.getValue();
                if (val == null) return null;
                return objectMapper.readValue(val, Object.class);
            }
        } catch (Exception e) {
            // fall through
        }
        return value;
    }

    private String extractNodeName(Object metadataPayload) {
        try {
            Object parsed = parseJson(metadataPayload);
            if (parsed instanceof Map<?, ?> map) {
                Object source = map.get("source");
                if (source instanceof String s) return s;
            }
        } catch (Exception e) {
            // fall through
        }
        return "unknown";
    }
}
