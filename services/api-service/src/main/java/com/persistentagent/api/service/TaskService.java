package com.persistentagent.api.service;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.persistentagent.api.config.ValidationConstants;
import com.persistentagent.api.exception.AgentNotFoundException;
import com.persistentagent.api.exception.InvalidStateTransitionException;
import com.persistentagent.api.exception.TaskNotFoundException;
import com.persistentagent.api.exception.ValidationException;
import com.persistentagent.api.model.request.TaskSubmissionRequest;
import com.persistentagent.api.model.response.*;
import com.persistentagent.api.repository.AgentRepository;
import com.persistentagent.api.repository.LangfuseEndpointRepository;
import com.persistentagent.api.repository.ModelRepository;
import com.persistentagent.api.repository.TaskRepository;
import com.persistentagent.api.service.observability.CheckpointCostTotals;
import com.persistentagent.api.service.observability.TaskObservabilityService;
import com.persistentagent.api.util.DateTimeUtil;
import com.persistentagent.api.util.JsonParseUtil;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Service;

import java.time.OffsetDateTime;
import java.util.*;
import java.util.stream.IntStream;

@Service
public class TaskService {

    private final TaskRepository taskRepository;
    private final AgentRepository agentRepository;
    private final ModelRepository modelRepository;
    private final LangfuseEndpointRepository langfuseEndpointRepository;
    private final TaskObservabilityService taskObservabilityService;
    private final ObjectMapper objectMapper;
    private final CheckpointEventParser checkpointEventParser;
    private final ConfigValidationHelper configValidationHelper;
    private final boolean devTaskControlsEnabled;

    public TaskService(
            TaskRepository taskRepository,
            AgentRepository agentRepository,
            ModelRepository modelRepository,
            LangfuseEndpointRepository langfuseEndpointRepository,
            TaskObservabilityService taskObservabilityService,
            ObjectMapper objectMapper,
            CheckpointEventParser checkpointEventParser,
            ConfigValidationHelper configValidationHelper,
            @Value("${app.dev-task-controls.enabled:false}") boolean devTaskControlsEnabled) {
        this.taskRepository = taskRepository;
        this.agentRepository = agentRepository;
        this.modelRepository = modelRepository;
        this.langfuseEndpointRepository = langfuseEndpointRepository;
        this.taskObservabilityService = taskObservabilityService;
        this.objectMapper = objectMapper;
        this.checkpointEventParser = checkpointEventParser;
        this.configValidationHelper = configValidationHelper;
        this.devTaskControlsEnabled = devTaskControlsEnabled;
    }

    public TaskSubmissionResponse submitTask(TaskSubmissionRequest request) {
        String tenantId = ValidationConstants.DEFAULT_TENANT_ID;
        String workerPoolId = ValidationConstants.DEFAULT_WORKER_POOL_ID;

        // 1. Validate task-level fields first (cheap, no DB needed)
        validateTaskTimeoutSeconds(request.taskTimeoutSeconds());
        if (request.langfuseEndpointId() != null) {
            langfuseEndpointRepository.findByIdAndTenant(request.langfuseEndpointId(), tenantId)
                    .orElseThrow(() -> new ValidationException(
                            "langfuse_endpoint_id not found: " + request.langfuseEndpointId()));
        }

        // 2. Apply task-level defaults
        int maxRetries = request.maxRetries() != null ? request.maxRetries() : ValidationConstants.DEFAULT_MAX_RETRIES;
        int maxSteps = request.maxSteps() != null ? request.maxSteps() : ValidationConstants.DEFAULT_MAX_STEPS;
        int taskTimeoutSeconds = request.taskTimeoutSeconds() != null
                ? request.taskTimeoutSeconds() : ValidationConstants.DEFAULT_TASK_TIMEOUT_SECONDS;

        // 3. Atomic agent resolution + model validation + task insertion (single SQL statement)
        //    The INSERT...SELECT joins agents with models to atomically enforce:
        //    - Agent exists and status = 'active'
        //    - Agent's model is active in the models registry
        //    This prevents both TOCTOU races from concurrent agent updates and
        //    enqueueing tasks against deactivated models.
        Optional<Map<String, Object>> result = taskRepository.insertTaskFromAgent(
                tenantId, request.agentId(), workerPoolId,
                request.input(), maxRetries, maxSteps, taskTimeoutSeconds,
                request.langfuseEndpointId());

        if (result.isEmpty()) {
            // Atomic INSERT returned empty — determine why for the error response.
            Optional<Map<String, Object>> agent = agentRepository.findByIdAndTenant(tenantId, request.agentId());
            if (agent.isEmpty()) {
                throw new AgentNotFoundException(request.agentId());
            }
            String agentStatus = (String) agent.get().get("status");
            if (!"active".equals(agentStatus)) {
                throw new ValidationException(
                        "Agent is disabled and cannot be used for task submission: " + request.agentId());
            }
            // Agent exists and is active, so the model must be deactivated
            throw new ValidationException(
                    "Agent's model is no longer active. Update the agent's model before submitting tasks: " + request.agentId());
        }

        Map<String, Object> row = result.get();
        UUID taskId = (UUID) row.get("task_id");
        String displayName = (String) row.get("agent_display_name_snapshot");
        OffsetDateTime createdAt = DateTimeUtil.toOffsetDateTime(row.get("created_at"));

        return new TaskSubmissionResponse(taskId, request.agentId(), displayName, "queued", createdAt);
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

        String agentDisplayName = (String) task.get("agent_display_name_snapshot");

        return new TaskStatusResponse(
                (UUID) task.get("task_id"),
                (String) task.get("agent_id"),
                agentDisplayName,
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
                DateTimeUtil.toOffsetDateTime(task.get("dead_lettered_at")),
                DateTimeUtil.toOffsetDateTime(task.get("created_at")),
                DateTimeUtil.toOffsetDateTime(task.get("updated_at")),
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
                            DateTimeUtil.toOffsetDateTime(row.get("created_at")));
                })
                .toList();

        return new CheckpointListResponse(checkpoints);
    }

    public TaskObservabilityResponse getTaskObservability(UUID taskId) {
        String tenantId = ValidationConstants.DEFAULT_TENANT_ID;
        Map<String, Object> task = taskRepository.findByIdWithAggregates(taskId, tenantId)
                .orElseThrow(() -> new TaskNotFoundException(taskId));

        String agentId = (String) task.get("agent_id");
        String agentDisplayName = (String) task.get("agent_display_name_snapshot");
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
                agentDisplayName,
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
                        (String) row.get("agent_display_name_snapshot"),
                        (String) row.get("dead_letter_reason"),
                        (String) row.get("last_error_code"),
                        (String) row.get("last_error_message"),
                        ((Number) row.get("retry_count")).intValue(),
                        (String) row.get("last_worker_id"),
                        DateTimeUtil.toOffsetDateTime(row.get("dead_lettered_at"))))
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
                            (String) row.get("agent_display_name_snapshot"),
                            (String) row.get("status"),
                            ((Number) row.get("retry_count")).intValue(),
                            ((Number) row.get("checkpoint_count")).intValue(),
                            asLong(row.get("total_cost_microdollars")),
                            DateTimeUtil.toOffsetDateTime(row.get("created_at")),
                            DateTimeUtil.toOffsetDateTime(row.get("updated_at")));
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
            OffsetDateTime first = DateTimeUtil.toOffsetDateTime(checkpointRows.get(0).get("created_at"));
            OffsetDateTime last = DateTimeUtil.toOffsetDateTime(checkpointRows.get(checkpointRows.size() - 1).get("created_at"));
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
            case "completed" -> DateTimeUtil.toOffsetDateTime(task.get("updated_at"));
            case "dead_letter" -> DateTimeUtil.toOffsetDateTime(task.get("dead_lettered_at"));
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
        OffsetDateTime createdAt = DateTimeUtil.toOffsetDateTime(row.get("created_at"));
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
