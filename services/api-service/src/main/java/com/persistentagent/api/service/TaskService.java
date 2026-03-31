package com.persistentagent.api.service;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.persistentagent.api.config.ValidationConstants;
import com.persistentagent.api.exception.InvalidStateTransitionException;
import com.persistentagent.api.exception.TaskNotFoundException;
import com.persistentagent.api.exception.ValidationException;
import com.persistentagent.api.model.request.AgentConfigRequest;
import com.persistentagent.api.model.request.TaskSubmissionRequest;
import com.persistentagent.api.model.response.*;
import com.persistentagent.api.repository.LangfuseEndpointRepository;
import com.persistentagent.api.repository.ModelRepository;
import com.persistentagent.api.repository.TaskRepository;
import com.persistentagent.api.service.observability.CheckpointCostTotals;
import com.persistentagent.api.service.observability.TaskObservabilityService;
import com.persistentagent.api.util.JsonParseUtil;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Service;

import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.sql.Timestamp;
import java.util.*;
import java.util.stream.IntStream;

@Service
public class TaskService {

    private final TaskRepository taskRepository;
    private final ModelRepository modelRepository;
    private final LangfuseEndpointRepository langfuseEndpointRepository;
    private final TaskObservabilityService taskObservabilityService;
    private final ObjectMapper objectMapper;
    private final CheckpointEventParser checkpointEventParser;
    private final boolean devTaskControlsEnabled;

    public TaskService(
            TaskRepository taskRepository,
            ModelRepository modelRepository,
            LangfuseEndpointRepository langfuseEndpointRepository,
            TaskObservabilityService taskObservabilityService,
            ObjectMapper objectMapper,
            CheckpointEventParser checkpointEventParser,
            @Value("${app.dev-task-controls.enabled:false}") boolean devTaskControlsEnabled) {
        this.taskRepository = taskRepository;
        this.modelRepository = modelRepository;
        this.langfuseEndpointRepository = langfuseEndpointRepository;
        this.taskObservabilityService = taskObservabilityService;
        this.objectMapper = objectMapper;
        this.checkpointEventParser = checkpointEventParser;
        this.devTaskControlsEnabled = devTaskControlsEnabled;
    }

    public TaskSubmissionResponse submitTask(TaskSubmissionRequest request) {
        // Additional validations beyond Bean Validation
        validateModel(request.agentConfig().provider(), request.agentConfig().model());
        validateAllowedTools(request.agentConfig().allowedTools());
        validateTaskTimeoutSeconds(request.taskTimeoutSeconds());

        String tenantId = ValidationConstants.DEFAULT_TENANT_ID;
        String workerPoolId = ValidationConstants.DEFAULT_WORKER_POOL_ID;

        // Validate langfuse_endpoint_id if provided
        if (request.langfuseEndpointId() != null) {
            langfuseEndpointRepository.findByIdAndTenant(request.langfuseEndpointId(), tenantId)
                    .orElseThrow(() -> new ValidationException(
                            "langfuse_endpoint_id not found: " + request.langfuseEndpointId()));
        }

        // Build agent_config_snapshot
        AgentConfigRequest agentConfigSnapshot = new AgentConfigRequest(
                request.agentConfig().systemPrompt(),
                request.agentConfig().provider(),
                request.agentConfig().model(),
                request.agentConfig().temperature() != null
                        ? request.agentConfig().temperature()
                        : ValidationConstants.DEFAULT_TEMPERATURE,
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
                request.input(), maxRetries, maxSteps, taskTimeoutSeconds,
                request.langfuseEndpointId());

        UUID taskId = (UUID) result.get("task_id");
        OffsetDateTime createdAt = toOffsetDateTime(result.get("created_at"));

        return new TaskSubmissionResponse(taskId, request.agentId(), "queued", createdAt);
    }

    public TaskStatusResponse getTaskStatus(UUID taskId) {
        String tenantId = ValidationConstants.DEFAULT_TENANT_ID;

        Map<String, Object> task = taskRepository.findByIdWithAggregates(taskId, tenantId)
                .orElseThrow(() -> new TaskNotFoundException(taskId));

        int checkpointCount = ((Number) task.get("checkpoint_count")).intValue();
        CheckpointCostTotals totals = taskObservabilityService.getTaskCostTotals(taskId, tenantId);

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
                totals.totalCostMicrodollars(),
                (String) task.get("lease_owner"),
                (String) task.get("last_error_code"),
                (String) task.get("last_error_message"),
                (String) task.get("last_worker_id"),
                (String) task.get("dead_letter_reason"),
                toOffsetDateTime(task.get("dead_lettered_at")),
                toOffsetDateTime(task.get("created_at")),
                toOffsetDateTime(task.get("updated_at")),
                (UUID) task.get("langfuse_endpoint_id"));
    }

    public CheckpointListResponse getCheckpoints(UUID taskId) {
        String tenantId = ValidationConstants.DEFAULT_TENANT_ID;

        List<Map<String, Object>> rows = taskRepository.getCheckpoints(taskId, tenantId)
                .orElseThrow(() -> new TaskNotFoundException(taskId));

        List<CheckpointResponse> checkpoints = IntStream.range(0, rows.size())
                .mapToObj(i -> {
                    Map<String, Object> row = rows.get(i);
                    String checkpointId = (String) row.get("checkpoint_id");
                    String nodeName = checkpointEventParser.extractNodeName(row.get("metadata_payload"), checkpointId);
                    Object executionMetadata = parseJson(row.get("execution_metadata"));
                    CheckpointEventResponse event = checkpointEventParser.parseEvent(
                            row.get("checkpoint_payload"),
                            row.get("metadata_payload"),
                            nodeName,
                            checkpointId);
                    return new CheckpointResponse(
                            checkpointId,
                            i + 1, // step_number derived from insertion order
                            nodeName,
                            (String) row.get("worker_id"),
                            ((Number) row.get("cost_microdollars")).intValue(),
                            executionMetadata,
                            event,
                            toOffsetDateTime(row.get("created_at")));
                })
                .toList();

        return new CheckpointListResponse(checkpoints);
    }

    public TaskObservabilityResponse getTaskObservability(UUID taskId) {
        String tenantId = ValidationConstants.DEFAULT_TENANT_ID;
        Map<String, Object> task = taskRepository.findByIdWithAggregates(taskId, tenantId)
                .orElseThrow(() -> new TaskNotFoundException(taskId));

        String agentId = (String) task.get("agent_id");
        String status = (String) task.get("status");

        RuntimeItemsResult result = buildRuntimeItems(taskId, task);
        CheckpointCostTotals totals = result.totals();

        List<TaskObservabilityItemResponse> items = new ArrayList<>(result.items());
        items.sort(Comparator
                .comparingInt((TaskObservabilityItemResponse item) -> isTerminalMarker(item.kind()) ? 1 : 0)
                .thenComparing(TaskObservabilityItemResponse::startedAt, Comparator.nullsLast(Comparator.naturalOrder()))
                .thenComparingInt(item -> observabilitySortOrder(item.kind()))
                .thenComparing(item -> Optional.ofNullable(item.stepNumber()).orElse(Integer.MAX_VALUE))
                .thenComparing(TaskObservabilityItemResponse::itemId));

        return new TaskObservabilityResponse(
                true,
                taskId,
                agentId,
                status,
                totals.totalCostMicrodollars(),
                totals.inputTokens(),
                totals.outputTokens(),
                totals.totalTokens(),
                totals.durationMs(),
                items
        );
    }

    public TaskCancelResponse cancelTask(UUID taskId) {
        String tenantId = ValidationConstants.DEFAULT_TENANT_ID;

        TaskRepository.MutationResult result = taskRepository.cancelTask(taskId, tenantId);
        return switch (result) {
            case UPDATED -> new TaskCancelResponse(taskId, "dead_letter", "cancelled_by_user");
            case NOT_FOUND -> throw new TaskNotFoundException(taskId);
            case WRONG_STATE -> throw new InvalidStateTransitionException(taskId,
                    "Task " + taskId + " cannot be cancelled (must be in queued or running state)");
        };
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
                        toOffsetDateTime(row.get("dead_lettered_at"))))
                .toList();

        return new DeadLetterListResponse(items);
    }

    public TaskListResponse listTasks(String status, String agentId, Integer limit) {
        String tenantId = ValidationConstants.DEFAULT_TENANT_ID;

        if (status != null && !status.isBlank()
                && !ValidationConstants.VALID_TASK_STATUSES.contains(status)) {
            throw new ValidationException("Invalid status filter: " + status
                    + ". Valid statuses: " + ValidationConstants.VALID_TASK_STATUSES);
        }

        int effectiveLimit = limit != null
                ? Math.min(Math.max(limit, 1), ValidationConstants.MAX_TASK_LIST_LIMIT)
                : ValidationConstants.DEFAULT_TASK_LIST_LIMIT;

        List<Map<String, Object>> rows = taskRepository.listTasks(tenantId, status, agentId, effectiveLimit);

        List<TaskSummaryResponse> items = rows.stream()
                .map(row -> {
                    return new TaskSummaryResponse(
                            (UUID) row.get("task_id"),
                            (String) row.get("agent_id"),
                            (String) row.get("status"),
                            ((Number) row.get("retry_count")).intValue(),
                            ((Number) row.get("checkpoint_count")).intValue(),
                            asLong(row.get("total_cost_microdollars")),
                            toOffsetDateTime(row.get("created_at")),
                            toOffsetDateTime(row.get("updated_at")));
                })
                .toList();

        return new TaskListResponse(items);
    }

    public RedriveResponse redriveTask(UUID taskId) {
        String tenantId = ValidationConstants.DEFAULT_TENANT_ID;

        TaskRepository.MutationResult result = taskRepository.redriveTask(taskId, tenantId);
        return switch (result) {
            case UPDATED -> new RedriveResponse(taskId, "queued");
            case NOT_FOUND -> throw new TaskNotFoundException(taskId);
            case WRONG_STATE -> throw new InvalidStateTransitionException(taskId,
                    "Task " + taskId + " cannot be redriven (must be in dead_letter state)");
        };
    }

    public HealthResponse getHealth() {
        boolean dbConnected = taskRepository.isDatabaseConnected();
        int activeWorkers = dbConnected ? taskRepository.getActiveWorkerCount() : 0;
        int queuedTasks = dbConnected ? taskRepository.getQueuedTaskCount() : 0;

        return new HealthResponse(
                dbConnected ? "healthy" : "unhealthy",
                dbConnected ? "connected" : "disconnected",
                activeWorkers,
                queuedTasks);
    }

    // --- Validation helpers ---

    private void validateModel(String provider, String model) {
        if (!modelRepository.isModelActive(provider, model)) {
            throw new ValidationException("Unsupported model or provider: " + provider + "/" + model
                    + ". Check GET /v1/models for supported ones.");
        }
    }

    private void validateAllowedTools(List<String> tools) {
        if (tools == null || tools.isEmpty()) {
            return; // no tools is valid
        }
        Set<String> allowedTools = new LinkedHashSet<>(ValidationConstants.ALLOWED_TOOLS);
        if (devTaskControlsEnabled) {
            allowedTools.addAll(ValidationConstants.DEV_TASK_CONTROL_TOOLS);
        }
        for (String tool : tools) {
            if (!allowedTools.contains(tool)) {
                throw new ValidationException("Unsupported tool: " + tool
                        + ". Allowed tools: " + allowedTools);
            }
        }
    }

    private void validateTaskTimeoutSeconds(Integer taskTimeoutSeconds) {
        if (taskTimeoutSeconds == null) {
            return;
        }

        int minimumTimeoutSeconds = devTaskControlsEnabled ? 1 : 60;
        if (taskTimeoutSeconds < minimumTimeoutSeconds || taskTimeoutSeconds > 86400) {
            throw new ValidationException(
                    "task_timeout_seconds must be between "
                            + minimumTimeoutSeconds
                            + " and 86400"
            );
        }
    }

    // --- Conversion helpers ---

    private OffsetDateTime toOffsetDateTime(Object value) {
        if (value == null)
            return null;
        if (value instanceof OffsetDateTime odt)
            return odt;
        if (value instanceof Timestamp ts)
            return ts.toInstant().atOffset(ZoneOffset.UTC);
        if (value instanceof java.util.Date d)
            return d.toInstant().atOffset(ZoneOffset.UTC);
        return null;
    }

    private long asLong(Object value) {
        if (value instanceof Number number) {
            return number.longValue();
        }
        return 0L;
    }

    private Object parseJson(Object value) {
        return JsonParseUtil.parseJson(objectMapper, value, "field", "n/a");
    }

    private List<Object> parseJsonList(Object value) {
        Object parsed = parseJson(value);
        if (parsed instanceof List<?> list) {
            return new ArrayList<>(list);
        }
        return List.of();
    }

    private RuntimeItemsResult buildRuntimeItems(UUID taskId, Map<String, Object> task) {
        List<TaskObservabilityItemResponse> items = new ArrayList<>();
        String agentId = (String) task.get("agent_id");
        String status = (String) task.get("status");

        List<Map<String, Object>> checkpointRows = taskRepository.getCheckpoints(taskId, ValidationConstants.DEFAULT_TENANT_ID)
                .orElse(List.of());
        List<CheckpointMarker> checkpointMarkers = IntStream.range(0, checkpointRows.size())
                .mapToObj(index -> checkpointMarker(taskId, agentId, index, checkpointRows.get(index)))
                .toList();
        items.addAll(checkpointMarkers.stream().map(CheckpointMarker::item).toList());

        // Aggregate cost totals from the same checkpoint data (avoids a second DB query)
        long totalCost = checkpointMarkers.stream().mapToLong(m -> m.item().costMicrodollars()).sum();
        int totalInput = checkpointMarkers.stream().mapToInt(m -> m.item().inputTokens()).sum();
        int totalOutput = checkpointMarkers.stream().mapToInt(m -> m.item().outputTokens()).sum();
        Long durationMs = null;
        if (checkpointRows.size() >= 2) {
            OffsetDateTime first = toOffsetDateTime(checkpointRows.get(0).get("created_at"));
            OffsetDateTime last = toOffsetDateTime(checkpointRows.get(checkpointRows.size() - 1).get("created_at"));
            if (first != null && last != null) {
                durationMs = java.time.Duration.between(first, last).toMillis();
            }
        }
        CheckpointCostTotals totals = new CheckpointCostTotals(totalCost, totalInput, totalOutput, totalInput + totalOutput, durationMs);

        List<OffsetDateTime> retryTimes = parseRetryTimes(task.get("retry_history"));
        for (int i = 0; i < retryTimes.size(); i++) {
            final int retryIndex = i;
            OffsetDateTime retryAt = retryTimes.get(i);
            OffsetDateTime nextRetryAt = i + 1 < retryTimes.size() ? retryTimes.get(i + 1) : null;
            checkpointMarkers.stream()
                    .filter(marker -> marker.item().startedAt() != null && marker.item().startedAt().isAfter(retryAt))
                    .filter(marker -> nextRetryAt == null || !marker.item().startedAt().isAfter(nextRetryAt))
                    .findFirst()
                    .ifPresent(marker -> items.add(new TaskObservabilityItemResponse(
                            "resume-%d".formatted(retryIndex + 1),
                            null,
                            "resumed_after_retry",
                            "Resumed from saved progress",
                            "Execution continued from the checkpoint saved after step %d.".formatted(Math.max(1, marker.stepNumber() - 1)),
                            Math.max(1, marker.stepNumber() - 1),
                            marker.nodeName(),
                            null,
                            null,
                            0L,
                            0,
                            0,
                            0,
                            null,
                            null,
                            null,
                            marker.item().startedAt(),
                            null
                    )));
        }

        OffsetDateTime lastCheckpointAt = checkpointMarkers.isEmpty()
                ? null
                : checkpointMarkers.get(checkpointMarkers.size() - 1).item().startedAt();
        OffsetDateTime terminalAt = switch (status) {
            case "completed" -> toOffsetDateTime(task.get("updated_at"));
            case "dead_letter" -> toOffsetDateTime(task.get("dead_lettered_at"));
            default -> null;
        };
        if (terminalAt != null && lastCheckpointAt != null && terminalAt.isBefore(lastCheckpointAt)) {
            terminalAt = lastCheckpointAt;
        }
        if (terminalAt != null) {
            String kind = "completed".equals(status) ? "completed" : "dead_lettered";
            String title = "completed".equals(status) ? "Execution completed" : "Execution failed";
            String summary = "completed".equals(status)
                    ? "Task execution finished successfully."
                    : buildDeadLetterSummary(task, checkpointMarkers);
            Integer lastStep = checkpointMarkers.isEmpty() ? null : checkpointMarkers.get(checkpointMarkers.size() - 1).stepNumber();
            String lastNode = checkpointMarkers.isEmpty() ? null : checkpointMarkers.get(checkpointMarkers.size() - 1).nodeName();
            items.add(new TaskObservabilityItemResponse(
                    "terminal-%s".formatted(kind),
                    null,
                    kind,
                    title,
                    summary,
                    lastStep,
                    lastNode,
                    null,
                    null,
                    0L,
                    0,
                    0,
                    0,
                    null,
                    null,
                    null,
                    terminalAt,
                    null
            ));
        }

        return new RuntimeItemsResult(items, totals);
    }

    private CheckpointMarker checkpointMarker(UUID taskId, String agentId, int index, Map<String, Object> row) {
        String checkpointId = (String) row.get("checkpoint_id");
        String nodeName = checkpointEventParser.extractNodeName(row.get("metadata_payload"), checkpointId);
        OffsetDateTime createdAt = toOffsetDateTime(row.get("created_at"));
        int stepNumber = index + 1;

        // Extract cost and token data from checkpoint row
        long costMicrodollars = row.get("cost_microdollars") instanceof Number n ? n.longValue() : 0L;
        int inputTokens = 0;
        int outputTokens = 0;
        String modelName = null;
        Object execMeta = row.get("execution_metadata");
        if (execMeta != null) {
            try {
                String json = execMeta.toString();
                var node = objectMapper.readTree(json);
                inputTokens = node.has("input_tokens") ? node.get("input_tokens").asInt(0) : 0;
                outputTokens = node.has("output_tokens") ? node.get("output_tokens").asInt(0) : 0;
                modelName = node.has("model") ? node.get("model").asText(null) : null;
            } catch (Exception e) {
                // Ignore parse errors — use defaults
            }
        }

        String title = modelName != null ? modelName : "Checkpoint saved";
        String summary = costMicrodollars > 0
                ? "%d in / %d out tokens — $%.4f".formatted(inputTokens, outputTokens, costMicrodollars / 1_000_000.0)
                : "Saved durable progress at step %d.".formatted(stepNumber);

        return new CheckpointMarker(
                stepNumber,
                nodeName,
                new TaskObservabilityItemResponse(
                        "checkpoint-%s".formatted(checkpointId),
                        null,
                        "checkpoint_persisted",
                        title,
                        summary,
                        stepNumber,
                        nodeName,
                        null,
                        modelName,
                        costMicrodollars,
                        inputTokens,
                        outputTokens,
                        inputTokens + outputTokens,
                        null,
                        null,
                        null,
                        createdAt,
                        null
                )
        );
    }

    private List<OffsetDateTime> parseRetryTimes(Object retryHistoryValue) {
        return parseJsonList(retryHistoryValue).stream()
                .map(Object::toString)
                .map(value -> {
                    try {
                        return OffsetDateTime.parse(value);
                    } catch (Exception e) {
                        return null;
                    }
                })
                .filter(Objects::nonNull)
                .sorted()
                .toList();
    }

    private String buildDeadLetterSummary(Map<String, Object> task, List<CheckpointMarker> checkpointMarkers) {
        String reason = (String) task.get("dead_letter_reason");
        String errorCode = (String) task.get("last_error_code");
        Integer lastStep = checkpointMarkers.isEmpty() ? null : checkpointMarkers.get(checkpointMarkers.size() - 1).stepNumber();
        String base = "Task moved to dead letter.";
        if (reason != null && !reason.isBlank()) {
            base = "Task moved to dead letter because %s.".formatted(reason.replace('_', ' '));
        }
        if (lastStep != null) {
            base += " Last durable checkpoint: step %d.".formatted(lastStep);
        }
        if (errorCode != null && !errorCode.isBlank()) {
            base += " Error code: %s.".formatted(errorCode);
        }
        return base;
    }

    private int observabilitySortOrder(String kind) {
        return switch (kind) {
            case "resumed_after_retry" -> 0;
            case "checkpoint_persisted" -> 1;
            case "completed", "dead_lettered" -> 2;
            default -> 3;
        };
    }

    private boolean isTerminalMarker(String kind) {
        return "completed".equals(kind) || "dead_lettered".equals(kind);
    }

    private record CheckpointMarker(int stepNumber, String nodeName, TaskObservabilityItemResponse item) {
    }

    private record RuntimeItemsResult(List<TaskObservabilityItemResponse> items, CheckpointCostTotals totals) {
    }
}
