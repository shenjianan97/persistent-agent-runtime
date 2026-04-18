package com.persistentagent.api.service;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.persistentagent.api.config.ValidationConstants;
import com.persistentagent.api.exception.AgentNotFoundException;
import com.persistentagent.api.model.ArtifactMetadata;
import com.persistentagent.api.repository.ArtifactRepository;
import com.persistentagent.api.exception.InvalidStateTransitionException;
import com.persistentagent.api.exception.TaskNotFoundException;
import com.persistentagent.api.exception.ValidationException;
import com.persistentagent.api.model.request.TaskSubmissionRequest;
import com.persistentagent.api.model.response.*;
import com.persistentagent.api.repository.AgentRepository;
import com.persistentagent.api.repository.LangfuseEndpointRepository;
import com.persistentagent.api.repository.ModelRepository;
import com.persistentagent.api.repository.TaskAttachedMemoryRepository;
import com.persistentagent.api.repository.TaskRepository;
import com.persistentagent.api.service.observability.CheckpointCostTotals;
import com.persistentagent.api.service.observability.TaskObservabilityService;
import com.persistentagent.api.util.DateTimeUtil;
import com.persistentagent.api.util.JsonParseUtil;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;
import org.springframework.web.multipart.MultipartFile;

import java.io.IOException;
import java.time.OffsetDateTime;
import java.util.*;
import java.util.stream.IntStream;

@Service
public class TaskService {

    private static final Set<String> VALID_PAUSE_REASONS = Set.of("budget_per_task", "budget_per_hour");

    /**
     * Maximum attached memory ids per task submission.
     *
     * <p>Provenance: NOT from the design doc — the design doc specifies only a
     * Console-side token-footprint indicator. This 50-id cap is a plan-level guard
     * (exec-plan Task 4) against blowing the initial prompt context regardless of
     * indicator state. Keep this limit in sync with the Console picker (Task 10).
     */
    static final int MAX_ATTACHED_MEMORY_IDS = 50;

    private final ArtifactRepository artifactRepository;
    private final TaskRepository taskRepository;
    private final AgentRepository agentRepository;
    private final ModelRepository modelRepository;
    private final LangfuseEndpointRepository langfuseEndpointRepository;
    private final TaskObservabilityService taskObservabilityService;
    private final TaskEventService taskEventService;
    private final ObjectMapper objectMapper;
    private final CheckpointEventParser checkpointEventParser;
    private final ConfigValidationHelper configValidationHelper;
    private final S3StorageService s3StorageService;
    private final TaskAttachedMemoryRepository taskAttachedMemoryRepository;
    private final boolean devTaskControlsEnabled;

    public TaskService(
            ArtifactRepository artifactRepository,
            TaskRepository taskRepository,
            AgentRepository agentRepository,
            ModelRepository modelRepository,
            LangfuseEndpointRepository langfuseEndpointRepository,
            TaskObservabilityService taskObservabilityService,
            TaskEventService taskEventService,
            ObjectMapper objectMapper,
            CheckpointEventParser checkpointEventParser,
            ConfigValidationHelper configValidationHelper,
            S3StorageService s3StorageService,
            TaskAttachedMemoryRepository taskAttachedMemoryRepository,
            @Value("${app.dev-task-controls.enabled:false}") boolean devTaskControlsEnabled) {
        this.artifactRepository = artifactRepository;
        this.taskRepository = taskRepository;
        this.agentRepository = agentRepository;
        this.modelRepository = modelRepository;
        this.langfuseEndpointRepository = langfuseEndpointRepository;
        this.taskObservabilityService = taskObservabilityService;
        this.taskEventService = taskEventService;
        this.objectMapper = objectMapper;
        this.checkpointEventParser = checkpointEventParser;
        this.configValidationHelper = configValidationHelper;
        this.s3StorageService = s3StorageService;
        this.taskAttachedMemoryRepository = taskAttachedMemoryRepository;
        this.devTaskControlsEnabled = devTaskControlsEnabled;
    }

    @Transactional
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

        // 2. Validate attached_memory_ids shape (cardinality + duplicates) before any DB work.
        //    Scope resolution happens AFTER the atomic agent-insert step so an unknown
        //    agent surfaces AgentNotFoundException rather than the generic attachment
        //    resolver error. The 404-not-403 rule still applies to the resolver step
        //    once we get there.
        List<UUID> attachedMemoryIds = validateAttachedMemoryIdShape(request.attachedMemoryIds());
        boolean skipMemoryWrite = Boolean.TRUE.equals(request.skipMemoryWrite());

        // 3. Apply task-level defaults
        int maxRetries = request.maxRetries() != null ? request.maxRetries() : ValidationConstants.DEFAULT_MAX_RETRIES;
        int maxSteps = request.maxSteps() != null ? request.maxSteps() : ValidationConstants.DEFAULT_MAX_STEPS;
        int taskTimeoutSeconds = request.taskTimeoutSeconds() != null
                ? request.taskTimeoutSeconds() : ValidationConstants.DEFAULT_TASK_TIMEOUT_SECONDS;

        // 4. Atomic agent resolution + model validation + task insertion (single SQL statement)
        //    The INSERT...SELECT joins agents with models to atomically enforce:
        //    - Agent exists and status = 'active'
        //    - Agent's model is active in the models registry
        //    This prevents both TOCTOU races from concurrent agent updates and
        //    enqueueing tasks against deactivated models.
        Optional<Map<String, Object>> result = taskRepository.insertTaskFromAgent(
                tenantId, request.agentId(), workerPoolId,
                request.input(), maxRetries, maxSteps, taskTimeoutSeconds,
                request.langfuseEndpointId(), skipMemoryWrite);

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

        // 5. Scope-validated resolution of attached memory ids in one SQL query.
        //    Runs after agent validation so unknown-agent errors win. Any count
        //    mismatch — unknown id, wrong tenant, or wrong agent — produces a
        //    uniform 4xx. Do NOT leak the offending id or the specific cause.
        //    Failure here rolls back the task insert via the @Transactional boundary.
        if (!attachedMemoryIds.isEmpty()) {
            List<UUID> resolved = taskAttachedMemoryRepository.resolveScopedMemoryIds(
                    tenantId, request.agentId(), attachedMemoryIds);
            if (resolved.size() != attachedMemoryIds.size()) {
                throw new ValidationException(
                        "one or more attached_memory_ids could not be resolved");
            }
        }

        // 6. Insert join-table rows preserving input order as `position`.
        //    Same transaction as the task insert — any failure rolls back the task.
        taskAttachedMemoryRepository.insertAttachments(taskId, attachedMemoryIds);

        // 7. Mirror attached_memory_ids into the task_submitted event's details JSONB.
        //    The join table is authoritative on divergence; the event mirror is a
        //    convenience so event consumers don't need to join. Always include the
        //    key — empty list renders as [], not an absent key.
        String eventDetails = buildTaskSubmittedEventDetails(attachedMemoryIds);
        taskEventService.recordEvent(tenantId, taskId, request.agentId(),
                "task_submitted", null, "queued", null, null, null, eventDetails);

        // Preview is empty for fresh submissions — memory rows referenced for the
        // first time by a just-inserted task are trivially all live, but we return
        // preview rows only on GET. The submission response mirrors the shape.
        List<AttachedMemoryPreview> preview = attachedMemoryIds.isEmpty()
                ? List.of()
                : taskAttachedMemoryRepository.findAttachedMemoriesPreview(
                        taskId, tenantId, request.agentId());

        return new TaskSubmissionResponse(
                taskId, request.agentId(), displayName, "queued", createdAt,
                List.copyOf(attachedMemoryIds), preview);
    }

    /**
     * Validates the shape of the {@code attached_memory_ids} payload:
     * cardinality cap, duplicate rejection. Syntactic UUID validity is enforced
     * upstream by Jackson; a non-UUID string fails deserialization with a 400.
     *
     * <p>Returns an immutable defensive copy of the list (never {@code null}).
     * Callers treat {@code null} and {@code []} identically.
     */
    private List<UUID> validateAttachedMemoryIdShape(List<UUID> attached) {
        if (attached == null || attached.isEmpty()) {
            return List.of();
        }
        if (attached.size() > MAX_ATTACHED_MEMORY_IDS) {
            throw new ValidationException(
                    "attached_memory_ids must not exceed " + MAX_ATTACHED_MEMORY_IDS + " entries");
        }
        // Reject duplicates — they would produce duplicate (task_id, memory_id) rows
        // which the PK rejects, but surface the error at the 400 layer with a clear message.
        Set<UUID> seen = new HashSet<>();
        for (UUID id : attached) {
            if (id == null) {
                throw new ValidationException("attached_memory_ids must not contain null");
            }
            if (!seen.add(id)) {
                throw new ValidationException(
                        "attached_memory_ids must not contain duplicate entries");
            }
        }
        return List.copyOf(attached);
    }

    /**
     * Builds the {@code task_submitted} event's {@code details} JSONB. Always
     * includes the {@code attached_memory_ids} key — empty list renders as {@code []},
     * not an absent key. Divergence from the join table is resolved in the join table's
     * favor per the design doc.
     */
    private String buildTaskSubmittedEventDetails(List<UUID> attachedMemoryIds) {
        try {
            Map<String, Object> details = new LinkedHashMap<>();
            details.put("attached_memory_ids",
                    attachedMemoryIds.stream().map(UUID::toString).toList());
            return objectMapper.writeValueAsString(details);
        } catch (Exception e) {
            // Serialization of a UUID list cannot fail in practice; fall back to a
            // minimal safe envelope so the event is still recorded.
            return "{\"attached_memory_ids\":[]}";
        }
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

        // Parse pending_approval_action from JSONB
        Object pendingApprovalAction = parseJson(task.get("pending_approval_action"));

        // Parse pause_details from JSONB
        Object pauseDetails = JsonParseUtil.parseJson(objectMapper, task.get("pause_details"), "pause_details",
                taskId.toString());

        List<ArtifactMetadata> artifacts = artifactRepository.findByTaskId(taskId, tenantId, "output");

        // Attached memories: the full id list (in position order), plus a preview
        // restricted to entries still resolvable within the task's scope. A fresh
        // task with no attachments gets empty lists rather than nulls — the design
        // doc specifies both fields are always present, even for legacy tasks.
        String agentId = (String) task.get("agent_id");
        List<UUID> attachedMemoryIds = taskAttachedMemoryRepository.findAttachedMemoryIds(taskId, tenantId, agentId);
        List<AttachedMemoryPreview> attachedMemoriesPreview = attachedMemoryIds.isEmpty()
                ? List.of()
                : taskAttachedMemoryRepository.findAttachedMemoriesPreview(taskId, tenantId, agentId);

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
                (UUID) task.get("langfuse_endpoint_id"),
                (String) task.get("pending_input_prompt"),
                pendingApprovalAction,
                DateTimeUtil.toOffsetDateTime(task.get("human_input_timeout_at")),
                (String) task.get("pause_reason"),
                pauseDetails,
                DateTimeUtil.toOffsetDateTime(task.get("resume_eligible_at")),
                artifacts,
                attachedMemoryIds,
                attachedMemoriesPreview);
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

    @Transactional
    public TaskCancelResponse cancelTask(UUID taskId) {
        String tenantId = ValidationConstants.DEFAULT_TENANT_ID;

        TaskRepository.CancelResult cancelResult = taskRepository.cancelTask(taskId, tenantId);
        return switch (cancelResult.outcome()) {
            case UPDATED -> {
                taskEventService.recordEvent(tenantId, taskId, cancelResult.agentId(),
                        "task_cancelled", cancelResult.previousStatus(), "dead_letter",
                        null, "cancelled_by_user", null, "{}");
                yield new TaskCancelResponse(taskId, "dead_letter", "cancelled_by_user");
            }
            case NOT_FOUND -> throw new TaskNotFoundException(taskId);
            case WRONG_STATE -> throw new InvalidStateTransitionException(taskId,
                    "Task " + taskId + " cannot be cancelled (must be in queued, running, waiting_for_approval, waiting_for_input, or paused state)");
        };
    }

    @Transactional
    public RedriveResponse approveTask(UUID taskId) {
        String tenantId = ValidationConstants.DEFAULT_TENANT_ID;

        TaskRepository.HitlMutationResult hitlResult = taskRepository.approveTask(taskId, tenantId);
        return switch (hitlResult.result()) {
            case UPDATED -> {
                taskRepository.notifyNewTask(hitlResult.workerPoolId());
                taskEventService.recordEvent(tenantId, taskId, hitlResult.agentId(),
                        "task_approved", "waiting_for_approval", "queued",
                        null, null, null, "{}");
                yield new RedriveResponse(taskId, "queued");
            }
            case NOT_FOUND -> throw new TaskNotFoundException(taskId);
            case WRONG_STATE -> throw new InvalidStateTransitionException(taskId,
                    "Task " + taskId + " cannot be approved (must be in waiting_for_approval state)");
        };
    }

    @Transactional
    public RedriveResponse rejectTask(UUID taskId, String reason) {
        String tenantId = ValidationConstants.DEFAULT_TENANT_ID;

        String humanResponse;
        String detailsJson;
        try {
            humanResponse = objectMapper.writeValueAsString(Map.of("kind", "approval", "approved", false, "reason", reason));
            detailsJson = objectMapper.writeValueAsString(Map.of("reason", reason));
        } catch (Exception e) {
            throw new RuntimeException("Failed to serialize rejection payload", e);
        }

        TaskRepository.HitlMutationResult hitlResult = taskRepository.rejectTask(taskId, tenantId, humanResponse);
        return switch (hitlResult.result()) {
            case UPDATED -> {
                taskRepository.notifyNewTask(hitlResult.workerPoolId());
                taskEventService.recordEvent(tenantId, taskId, hitlResult.agentId(),
                        "task_rejected", "waiting_for_approval", "queued",
                        null, null, null, detailsJson);
                yield new RedriveResponse(taskId, "queued");
            }
            case NOT_FOUND -> throw new TaskNotFoundException(taskId);
            case WRONG_STATE -> throw new InvalidStateTransitionException(taskId,
                    "Task " + taskId + " cannot be rejected (must be in waiting_for_approval state)");
        };
    }

    @Transactional
    public RedriveResponse respondToTask(UUID taskId, String message) {
        String tenantId = ValidationConstants.DEFAULT_TENANT_ID;

        String humanResponse;
        String detailsJson;
        try {
            humanResponse = objectMapper.writeValueAsString(Map.of("kind", "input", "message", message));
            detailsJson = objectMapper.writeValueAsString(Map.of("message", message));
        } catch (Exception e) {
            throw new RuntimeException("Failed to serialize response payload", e);
        }

        TaskRepository.HitlMutationResult hitlResult = taskRepository.respondToTask(taskId, tenantId, humanResponse);
        return switch (hitlResult.result()) {
            case UPDATED -> {
                taskRepository.notifyNewTask(hitlResult.workerPoolId());
                taskEventService.recordEvent(tenantId, taskId, hitlResult.agentId(),
                        "task_input_received", "waiting_for_input", "queued",
                        null, null, null, detailsJson);
                yield new RedriveResponse(taskId, "queued");
            }
            case NOT_FOUND -> throw new TaskNotFoundException(taskId);
            case WRONG_STATE -> throw new InvalidStateTransitionException(taskId,
                    "Task " + taskId + " cannot receive input (must be in waiting_for_input state)");
        };
    }

    @Transactional
    public RedriveResponse followUpTask(UUID taskId, String input) {
        String tenantId = ValidationConstants.DEFAULT_TENANT_ID;

        String humanResponse;
        String detailsJson;
        try {
            humanResponse = objectMapper.writeValueAsString(Map.of("kind", "follow_up", "message", input));
            detailsJson = objectMapper.writeValueAsString(Map.of("input", input));
        } catch (Exception e) {
            throw new RuntimeException("Failed to serialize follow-up payload", e);
        }

        TaskRepository.HitlMutationResult result = taskRepository.followUpTask(taskId, tenantId, humanResponse);
        return switch (result.result()) {
            case UPDATED -> {
                taskEventService.recordEvent(tenantId, taskId, result.agentId(),
                        "task_follow_up", "completed", "queued",
                        null, null, null, detailsJson);
                yield new RedriveResponse(taskId, "queued");
            }
            case NOT_FOUND -> throw new TaskNotFoundException(taskId);
            case WRONG_STATE -> throw new InvalidStateTransitionException(taskId,
                    "Task " + taskId + " cannot be followed up (must be in completed state)");
        };
    }

    @Transactional
    public RedriveResponse resumeTask(UUID taskId) {
        String tenantId = ValidationConstants.DEFAULT_TENANT_ID;

        TaskRepository.ResumeMutationResult resumeResult = taskRepository.resumeTask(taskId, tenantId);
        return switch (resumeResult.outcome()) {
            case UPDATED -> {
                taskRepository.notifyNewTask(resumeResult.workerPoolId());
                String detailsJson;
                try {
                    detailsJson = objectMapper.writeValueAsString(Map.of(
                            "resume_trigger", "manual_operator_resume",
                            "budget_max_per_task_at_resume", resumeResult.budgetMax(),
                            "task_cost_microdollars", resumeResult.taskCost()));
                } catch (Exception e) {
                    detailsJson = "{}";
                }
                taskEventService.recordEvent(tenantId, taskId, resumeResult.agentId(),
                        "task_resumed", "paused", "queued",
                        null, null, null, detailsJson);
                yield new RedriveResponse(taskId, "queued");
            }
            case NOT_FOUND -> throw new TaskNotFoundException(taskId);
            case WRONG_STATE -> {
                // Differentiated 409 messages based on diagnostic fields
                if (!"paused".equals(resumeResult.currentStatus())) {
                    throw new InvalidStateTransitionException(taskId,
                            "Task " + taskId + " is not paused (current status: " + resumeResult.currentStatus() + ")");
                }
                if (!"active".equals(resumeResult.agentStatus())) {
                    throw new InvalidStateTransitionException(taskId,
                            "Task " + taskId + " cannot be resumed because the agent is disabled");
                }
                if (resumeResult.taskCost() != null && resumeResult.budgetMax() != null
                        && resumeResult.taskCost() > resumeResult.budgetMax()) {
                    throw new InvalidStateTransitionException(taskId,
                            "Task " + taskId + " cost (" + resumeResult.taskCost()
                                    + ") still exceeds budget (" + resumeResult.budgetMax()
                                    + "). Increase budget_max_per_task first.");
                }
                // Generic fallback
                throw new InvalidStateTransitionException(taskId,
                        "Task " + taskId + " cannot be resumed");
            }
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

    public TaskListResponse listTasks(String status, String agentId, String pauseReason, Integer limit) {
        String tenantId = ValidationConstants.DEFAULT_TENANT_ID;

        if (status != null && !status.isBlank()
                && !ValidationConstants.VALID_TASK_STATUSES.contains(status)) {
            throw new ValidationException("Invalid status filter: " + status
                    + ". Valid statuses: " + ValidationConstants.VALID_TASK_STATUSES);
        }

        if (pauseReason != null && !pauseReason.isBlank()
                && !VALID_PAUSE_REASONS.contains(pauseReason)) {
            throw new ValidationException("Invalid pause_reason filter: " + pauseReason
                    + ". Valid pause reasons: " + VALID_PAUSE_REASONS);
        }

        int effectiveLimit = limit != null
                ? Math.min(Math.max(limit, 1), ValidationConstants.MAX_TASK_LIST_LIMIT)
                : ValidationConstants.DEFAULT_TASK_LIST_LIMIT;

        List<Map<String, Object>> rows = taskRepository.listTasks(tenantId, status, agentId, pauseReason, effectiveLimit);

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
                            DateTimeUtil.toOffsetDateTime(row.get("updated_at")),
                            (String) row.get("pause_reason"),
                            DateTimeUtil.toOffsetDateTime(row.get("resume_eligible_at")));
                })
                .toList();

        return new TaskListResponse(items);
    }

    @Transactional
    public RedriveResponse redriveTask(UUID taskId) {
        String tenantId = ValidationConstants.DEFAULT_TENANT_ID;

        TaskRepository.RedriveResult redriveResult = taskRepository.redriveTask(taskId, tenantId);
        return switch (redriveResult.outcome()) {
            case UPDATED -> {
                taskEventService.recordEvent(tenantId, taskId, redriveResult.agentId(),
                        "task_redriven", "dead_letter", "queued",
                        null, null, null, "{}");
                yield new RedriveResponse(taskId, "queued");
            }
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

    @Transactional
    public TaskSubmissionResponse submitTaskWithFiles(
            TaskSubmissionRequest request, List<MultipartFile> files) {
        // First, submit the task normally (creates the task row)
        TaskSubmissionResponse response = submitTask(request);
        UUID taskId = response.taskId();
        String tenantId = ValidationConstants.DEFAULT_TENANT_ID;

        // If files are present, validate sandbox requirement and upload
        if (files != null && !files.isEmpty()) {
            // Look up the agent to check sandbox config
            Map<String, Object> agentRow = agentRepository.findByIdAndTenant(
                    tenantId, request.agentId())
                    .orElseThrow(() -> new ValidationException(
                            "Agent not found: " + request.agentId()));

            String agentConfigJson = (String) agentRow.get("agent_config");
            boolean sandboxEnabled = isSandboxEnabled(agentConfigJson);

            if (!sandboxEnabled) {
                throw new ValidationException(
                        "File attachments require an agent with sandbox enabled. "
                        + "Agent '" + request.agentId() + "' does not have sandbox.enabled: true.");
            }

            // Upload each file to S3 and record as input artifact.
            // Track uploaded S3 keys so we can clean up orphans if a later step fails
            // (DB rolls back on exception but S3 writes do not).
            List<String> uploadedS3Keys = new ArrayList<>();
            try {
                for (MultipartFile file : files) {
                    String filename = file.getOriginalFilename();
                    if (filename == null || filename.isBlank()) {
                        filename = "unnamed_file";
                    }

                    String s3Key = tenantId + "/" + taskId + "/input/" + filename;
                    String contentType = file.getContentType();
                    if (contentType == null || contentType.isBlank()) {
                        contentType = "application/octet-stream";
                    }

                    try {
                        byte[] data = file.getBytes();
                        s3StorageService.upload(s3Key, data, contentType);
                        uploadedS3Keys.add(s3Key);
                        artifactRepository.insert(
                                taskId, tenantId, filename, "input",
                                contentType, data.length, s3Key);
                    } catch (IOException e) {
                        throw new RuntimeException(
                                "Failed to read uploaded file: " + filename, e);
                    }
                }
            } catch (Exception e) {
                // Best-effort cleanup: delete any S3 objects already uploaded.
                // The DB transaction will roll back automatically, but S3 is not transactional.
                for (String orphanKey : uploadedS3Keys) {
                    try {
                        s3StorageService.delete(orphanKey);
                    } catch (Exception cleanupEx) {
                        // Log but don't mask the original exception
                    }
                }
                throw e;
            }
        }

        return response;
    }

    private boolean isSandboxEnabled(String agentConfigJson) {
        try {
            var configNode = objectMapper.readTree(agentConfigJson);
            var sandboxNode = configNode.get("sandbox");
            if (sandboxNode == null || sandboxNode.isNull()) {
                return false;
            }
            var enabledNode = sandboxNode.get("enabled");
            return enabledNode != null && enabledNode.asBoolean(false);
        } catch (Exception e) {
            return false;
        }
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
