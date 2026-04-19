package com.persistentagent.api.service;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.persistentagent.api.config.ValidationConstants;
import com.persistentagent.api.exception.AgentNotFoundException;
import com.persistentagent.api.exception.ValidationException;
import com.persistentagent.api.model.request.AgentConfigRequest;
import com.persistentagent.api.model.request.AgentCreateRequest;
import com.persistentagent.api.model.request.AgentUpdateRequest;
import com.persistentagent.api.model.request.ContextManagementConfigRequest;
import com.persistentagent.api.model.request.MemoryConfigRequest;
import com.persistentagent.api.model.request.SandboxConfigRequest;
import com.persistentagent.api.model.response.AgentResponse;
import com.persistentagent.api.model.response.AgentSummaryResponse;
import com.persistentagent.api.repository.AgentRepository;
import com.persistentagent.api.util.DateTimeUtil;
import com.persistentagent.api.util.JsonParseUtil;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.time.OffsetDateTime;
import java.util.List;
import java.util.Map;
import java.util.UUID;

@Service
public class AgentService {

    private static final int DEFAULT_MAX_CONCURRENT_TASKS = 5;
    private static final long DEFAULT_BUDGET_MAX_PER_TASK = 500000L;
    private static final long DEFAULT_BUDGET_MAX_PER_HOUR = 5000000L;

    private final AgentRepository agentRepository;
    private final ConfigValidationHelper configValidationHelper;
    private final ObjectMapper objectMapper;
    private final boolean devTaskControlsEnabled;

    public AgentService(AgentRepository agentRepository,
            ConfigValidationHelper configValidationHelper,
            ObjectMapper objectMapper,
            @Value("${app.dev-task-controls.enabled:false}") boolean devTaskControlsEnabled) {
        this.agentRepository = agentRepository;
        this.configValidationHelper = configValidationHelper;
        this.objectMapper = objectMapper;
        this.devTaskControlsEnabled = devTaskControlsEnabled;
    }

    @Transactional
    public AgentResponse createAgent(AgentCreateRequest request) {
        configValidationHelper.validateAgentConfig(request.agentConfig());

        AgentConfigRequest canonicalized = canonicalizeConfig(request.agentConfig());
        String agentConfigJson = serializeConfig(canonicalized);

        String tenantId = ValidationConstants.DEFAULT_TENANT_ID;
        String agentId = UUID.randomUUID().toString();

        int maxConcurrentTasks = request.maxConcurrentTasks() != null
                ? request.maxConcurrentTasks() : DEFAULT_MAX_CONCURRENT_TASKS;
        long budgetMaxPerTask = request.budgetMaxPerTask() != null
                ? request.budgetMaxPerTask() : DEFAULT_BUDGET_MAX_PER_TASK;
        long budgetMaxPerHour = request.budgetMaxPerHour() != null
                ? request.budgetMaxPerHour() : DEFAULT_BUDGET_MAX_PER_HOUR;

        Map<String, Object> result = agentRepository.insert(
                tenantId, agentId, request.displayName(), agentConfigJson,
                maxConcurrentTasks, budgetMaxPerTask, budgetMaxPerHour);

        // Create agent_runtime_state row in the same transaction
        agentRepository.insertRuntimeState(tenantId, agentId);

        OffsetDateTime createdAt = DateTimeUtil.toOffsetDateTime(result.get("created_at"));
        OffsetDateTime updatedAt = DateTimeUtil.toOffsetDateTime(result.get("updated_at"));

        Object configObj = JsonParseUtil.parseJson(objectMapper, agentConfigJson, "agent_config", agentId);

        return new AgentResponse(
                agentId,
                request.displayName(),
                configObj,
                ValidationConstants.AGENT_STATUS_ACTIVE,
                maxConcurrentTasks,
                budgetMaxPerTask,
                budgetMaxPerHour,
                createdAt,
                updatedAt);
    }

    public AgentResponse getAgent(String agentId) {
        String tenantId = ValidationConstants.DEFAULT_TENANT_ID;

        Map<String, Object> row = agentRepository.findByIdAndTenant(tenantId, agentId)
                .orElseThrow(() -> new AgentNotFoundException(agentId));

        return toAgentResponse(row);
    }

    public List<AgentSummaryResponse> listAgents(String status, Integer limit) {
        String tenantId = ValidationConstants.DEFAULT_TENANT_ID;

        if (status != null && !status.isBlank()
                && !ValidationConstants.VALID_AGENT_STATUSES.contains(status)) {
            throw new ValidationException("Invalid status filter: " + status
                    + ". Valid statuses: " + ValidationConstants.VALID_AGENT_STATUSES);
        }

        int effectiveLimit = limit != null
                ? Math.min(Math.max(limit, 1), ValidationConstants.MAX_AGENT_LIST_LIMIT)
                : ValidationConstants.DEFAULT_AGENT_LIST_LIMIT;

        List<Map<String, Object>> rows = agentRepository.listByTenant(tenantId, status, effectiveLimit);

        return rows.stream()
                .map(row -> new AgentSummaryResponse(
                        (String) row.get("agent_id"),
                        (String) row.get("display_name"),
                        (String) row.get("provider"),
                        (String) row.get("model"),
                        (String) row.get("status"),
                        ((Number) row.get("max_concurrent_tasks")).intValue(),
                        ((Number) row.get("budget_max_per_task")).longValue(),
                        ((Number) row.get("budget_max_per_hour")).longValue(),
                        DateTimeUtil.toOffsetDateTime(row.get("created_at")),
                        DateTimeUtil.toOffsetDateTime(row.get("updated_at"))))
                .toList();
    }

    public AgentResponse updateAgent(String agentId, AgentUpdateRequest request) {
        String tenantId = ValidationConstants.DEFAULT_TENANT_ID;

        // Validate status
        if (!ValidationConstants.VALID_AGENT_STATUSES.contains(request.status())) {
            throw new ValidationException("Invalid status: " + request.status()
                    + ". Valid statuses: " + ValidationConstants.VALID_AGENT_STATUSES);
        }

        configValidationHelper.validateAgentConfig(request.agentConfig());

        AgentConfigRequest canonicalized = canonicalizeConfig(request.agentConfig());
        String agentConfigJson = serializeConfig(canonicalized);

        // For update, we need current values as defaults if not provided
        Map<String, Object> existing = agentRepository.findByIdAndTenant(tenantId, agentId)
                .orElseThrow(() -> new AgentNotFoundException(agentId));

        int maxConcurrentTasks = request.maxConcurrentTasks() != null
                ? request.maxConcurrentTasks()
                : ((Number) existing.get("max_concurrent_tasks")).intValue();
        long budgetMaxPerTask = request.budgetMaxPerTask() != null
                ? request.budgetMaxPerTask()
                : ((Number) existing.get("budget_max_per_task")).longValue();
        long budgetMaxPerHour = request.budgetMaxPerHour() != null
                ? request.budgetMaxPerHour()
                : ((Number) existing.get("budget_max_per_hour")).longValue();

        Map<String, Object> row = agentRepository.update(
                tenantId, agentId, request.displayName(), agentConfigJson, request.status(),
                maxConcurrentTasks, budgetMaxPerTask, budgetMaxPerHour)
                .orElseThrow(() -> new AgentNotFoundException(agentId));

        return toAgentResponse(row);
    }

    // --- Config canonicalization ---

    private AgentConfigRequest canonicalizeConfig(AgentConfigRequest config) {
        SandboxConfigRequest sandbox = config.sandbox();
        SandboxConfigRequest canonicalizedSandbox = null;
        if (sandbox != null) {
            boolean enabled = sandbox.enabled() != null && sandbox.enabled();
            canonicalizedSandbox = new SandboxConfigRequest(
                    enabled,
                    enabled ? sandbox.template() : null,
                    enabled ? (sandbox.vcpu() != null ? sandbox.vcpu() : ValidationConstants.SANDBOX_VCPU_DEFAULT) : null,
                    enabled ? (sandbox.memoryMb() != null ? sandbox.memoryMb() : ValidationConstants.SANDBOX_MEMORY_MB_DEFAULT) : null,
                    enabled ? (sandbox.timeoutSeconds() != null ? sandbox.timeoutSeconds() : ValidationConstants.SANDBOX_TIMEOUT_SECONDS_DEFAULT) : null
            );
        }

        // Auto-determine allowed tools based on agent config
        boolean sandboxEnabled = canonicalizedSandbox != null
                && canonicalizedSandbox.enabled() != null
                && canonicalizedSandbox.enabled();

        List<String> canonicalizedTools = new java.util.ArrayList<>(
                ValidationConstants.BASE_PLATFORM_TOOLS);
        if (sandboxEnabled) {
            canonicalizedTools.addAll(ValidationConstants.SANDBOX_TOOLS);
        }
        // Preserve dev-only tools explicitly requested by the caller (e.g. dev_sleep)
        // when dev task controls are enabled. These are not added by default to avoid
        // confusing production agents.
        if (devTaskControlsEnabled && config.allowedTools() != null) {
            for (String tool : config.allowedTools()) {
                if (ValidationConstants.DEV_TASK_CONTROL_TOOLS.contains(tool)
                        && !canonicalizedTools.contains(tool)) {
                    canonicalizedTools.add(tool);
                }
            }
        }

        // Memory sub-object round-trip: preserve verbatim when present, omit
        // when absent. No platform defaults are written into the canonical
        // config — defaults apply at read time (worker + validator) per
        // Phase 2 Track 5 design.
        MemoryConfigRequest canonicalizedMemory = config.memory();

        // Context-management sub-object round-trip: preserve verbatim when present,
        // omit when absent. Same pattern as memory above. No platform defaults are
        // written into the canonical config — defaults apply at read time in the
        // worker (Task 3) per Phase 2 Track 7 design.
        ContextManagementConfigRequest canonicalizedContextManagement = config.contextManagement();

        return new AgentConfigRequest(
                config.systemPrompt(),
                config.provider(),
                config.model(),
                config.temperature() != null
                        ? config.temperature()
                        : ValidationConstants.DEFAULT_TEMPERATURE,
                canonicalizedTools,
                config.toolServers() != null
                        ? config.toolServers()
                        : List.of(),
                canonicalizedSandbox,
                canonicalizedMemory,
                canonicalizedContextManagement);
    }

    private String serializeConfig(AgentConfigRequest config) {
        try {
            return objectMapper.writeValueAsString(config);
        } catch (JsonProcessingException e) {
            throw new ValidationException("Failed to serialize agent_config: " + e.getMessage());
        }
    }

    // --- Conversion helpers ---

    private AgentResponse toAgentResponse(Map<String, Object> row) {
        Object agentConfig = JsonParseUtil.parseJson(objectMapper, row.get("agent_config"), "agent_config",
                (String) row.get("agent_id"));
        return new AgentResponse(
                (String) row.get("agent_id"),
                (String) row.get("display_name"),
                agentConfig,
                (String) row.get("status"),
                ((Number) row.get("max_concurrent_tasks")).intValue(),
                ((Number) row.get("budget_max_per_task")).longValue(),
                ((Number) row.get("budget_max_per_hour")).longValue(),
                DateTimeUtil.toOffsetDateTime(row.get("created_at")),
                DateTimeUtil.toOffsetDateTime(row.get("updated_at")));
    }

}
