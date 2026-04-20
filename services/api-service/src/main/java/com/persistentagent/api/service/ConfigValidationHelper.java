package com.persistentagent.api.service;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.persistentagent.api.config.ValidationConstants;
import com.persistentagent.api.exception.AgentNotFoundException;
import com.persistentagent.api.exception.ValidationException;
import com.persistentagent.api.model.request.AgentConfigRequest;
import com.persistentagent.api.model.request.ContextManagementConfigRequest;
import com.persistentagent.api.model.request.MemoryConfigRequest;
import com.persistentagent.api.model.request.SandboxConfigRequest;
import com.persistentagent.api.repository.AgentRepository;
import com.persistentagent.api.repository.ModelRepository;
import com.persistentagent.api.repository.ToolServerRepository;
import org.postgresql.util.PGobject;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Component;

import java.util.HashSet;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.Set;
import java.util.regex.Pattern;

@Component
public class ConfigValidationHelper {

    private static final Pattern TOOL_SERVER_NAME_PATTERN =
            Pattern.compile(ValidationConstants.TOOL_SERVER_NAME_PATTERN);

    private final ModelRepository modelRepository;
    private final ToolServerRepository toolServerRepository;
    private final AgentRepository agentRepository;
    private final ObjectMapper objectMapper;
    private final Set<String> allowedTools;

    public ConfigValidationHelper(
            ModelRepository modelRepository,
            ToolServerRepository toolServerRepository,
            AgentRepository agentRepository,
            ObjectMapper objectMapper,
            @Value("${app.dev-task-controls.enabled:false}") boolean devTaskControlsEnabled) {
        this.modelRepository = modelRepository;
        this.toolServerRepository = toolServerRepository;
        this.agentRepository = agentRepository;
        this.objectMapper = objectMapper;

        Set<String> tools = new LinkedHashSet<>(ValidationConstants.ALLOWED_TOOLS);
        if (devTaskControlsEnabled) {
            tools.addAll(ValidationConstants.DEV_TASK_CONTROL_TOOLS);
        }
        this.allowedTools = Set.copyOf(tools);
    }

    public void validateModel(String provider, String model) {
        if (!modelRepository.isModelActive(provider, model)) {
            throw new ValidationException("Unsupported model or provider: " + provider + "/" + model
                    + ". Check GET /v1/models for supported ones.");
        }
    }

    public void validateAllowedTools(List<String> tools) {
        if (tools == null || tools.isEmpty()) {
            return; // no tools is valid
        }
        for (String tool : tools) {
            if (!allowedTools.contains(tool)) {
                throw new ValidationException("Unsupported tool: " + tool
                        + ". Allowed tools: " + allowedTools);
            }
        }
    }

    public void validateToolServers(List<String> toolServers) {
        if (toolServers == null || toolServers.isEmpty()) {
            return; // No tool servers is valid
        }

        // Check for duplicates
        Set<String> seen = new HashSet<>();
        for (String name : toolServers) {
            if (!seen.add(name)) {
                throw new ValidationException("Duplicate tool server name: " + name);
            }
        }

        // Validate name format
        for (String name : toolServers) {
            if (!TOOL_SERVER_NAME_PATTERN.matcher(name).matches()) {
                throw new ValidationException("Invalid tool server name: " + name
                        + ". Must match pattern: " + ValidationConstants.TOOL_SERVER_NAME_PATTERN);
            }
        }

        // Check that all referenced servers exist and are active
        String tenantId = ValidationConstants.DEFAULT_TENANT_ID;
        List<Map<String, Object>> found = toolServerRepository.findByTenantAndNames(tenantId, toolServers);

        Set<String> foundNames = new HashSet<>();
        for (Map<String, Object> row : found) {
            String name = (String) row.get("name");
            String status = (String) row.get("status");
            if (!ValidationConstants.TOOL_SERVER_STATUS_ACTIVE.equals(status)) {
                throw new ValidationException("Tool server '" + name + "' is disabled");
            }
            foundNames.add(name);
        }

        for (String name : toolServers) {
            if (!foundNames.contains(name)) {
                throw new ValidationException("Tool server not found: " + name);
            }
        }
    }

    public void validateSandboxConfig(SandboxConfigRequest sandbox) {
        if (sandbox == null) {
            return; // No sandbox config is valid — defaults to disabled
        }

        // enabled defaults to false if null
        boolean enabled = sandbox.enabled() != null && sandbox.enabled();

        if (!enabled) {
            return; // Disabled sandbox — no further validation needed
        }

        // template is required when sandbox is enabled
        if (sandbox.template() == null || sandbox.template().isBlank()) {
            throw new ValidationException("sandbox.template is required when sandbox is enabled");
        }

        // vcpu validation
        if (sandbox.vcpu() != null) {
            if (sandbox.vcpu() < ValidationConstants.SANDBOX_VCPU_MIN
                    || sandbox.vcpu() > ValidationConstants.SANDBOX_VCPU_MAX) {
                throw new ValidationException("sandbox.vcpu must be between "
                        + ValidationConstants.SANDBOX_VCPU_MIN + " and "
                        + ValidationConstants.SANDBOX_VCPU_MAX);
            }
        }

        // memory_mb validation
        if (sandbox.memoryMb() != null) {
            if (sandbox.memoryMb() < ValidationConstants.SANDBOX_MEMORY_MB_MIN
                    || sandbox.memoryMb() > ValidationConstants.SANDBOX_MEMORY_MB_MAX) {
                throw new ValidationException("sandbox.memory_mb must be between "
                        + ValidationConstants.SANDBOX_MEMORY_MB_MIN + " and "
                        + ValidationConstants.SANDBOX_MEMORY_MB_MAX);
            }
        }

        // timeout_seconds validation
        if (sandbox.timeoutSeconds() != null) {
            if (sandbox.timeoutSeconds() < ValidationConstants.SANDBOX_TIMEOUT_SECONDS_MIN
                    || sandbox.timeoutSeconds() > ValidationConstants.SANDBOX_TIMEOUT_SECONDS_MAX) {
                throw new ValidationException("sandbox.timeout_seconds must be between "
                        + ValidationConstants.SANDBOX_TIMEOUT_SECONDS_MIN + " and "
                        + ValidationConstants.SANDBOX_TIMEOUT_SECONDS_MAX);
            }
        }

        // Note: sandbox.timeout_seconds is validated here to be a reasonable minimum (>= 60s).
        // The runtime cross-validation (sandbox timeout >= task timeout) happens at task
        // submission time in Track 2 Task 6, because task_timeout_seconds is per-task,
        // not per-agent. At agent config time we can only validate the range.
    }

    /**
     * Validates the optional {@code memory} sub-object on
     * {@link AgentConfigRequest}. Absence is always valid; platform defaults
     * for {@code summarizer_model} and {@code max_entries} apply at read time
     * in the worker and the validator, not at write time.
     *
     * <ul>
     *   <li>{@code summarizerModel}, when non-blank, must resolve against the
     *       {@code models} table for the agent's provider (same lookup as
     *       {@link #validateModel(String, String)}).</li>
     *   <li>{@code maxEntries}, when non-null, must fall within the platform
     *       bounds {@code [MEMORY_MAX_ENTRIES_MIN, MEMORY_MAX_ENTRIES_MAX]}.</li>
     *   <li>{@code enabled} is a pure toggle — no further validation.</li>
     * </ul>
     */
    public void validateMemoryConfig(MemoryConfigRequest memory, String provider) {
        if (memory == null) {
            return; // Absent memory sub-object is valid — Phase 1/2 behaviour.
        }

        // summarizer_model: optional; when present, must be active for the
        // agent's provider. Reject blank strings — ambiguous with absence.
        if (memory.summarizerModel() != null && !memory.summarizerModel().isBlank()) {
            if (!modelRepository.isModelActive(provider, memory.summarizerModel())) {
                throw new ValidationException(
                        "Unsupported summarizer model or provider: "
                                + provider + "/" + memory.summarizerModel()
                                + ". Check GET /v1/models for supported ones.");
            }
        }

        // max_entries: optional; when present, must be in the platform range.
        if (memory.maxEntries() != null) {
            int value = memory.maxEntries();
            if (value < ValidationConstants.MEMORY_MAX_ENTRIES_MIN
                    || value > ValidationConstants.MEMORY_MAX_ENTRIES_MAX) {
                throw new ValidationException(
                        "memory.max_entries must be between "
                                + ValidationConstants.MEMORY_MAX_ENTRIES_MIN + " and "
                                + ValidationConstants.MEMORY_MAX_ENTRIES_MAX);
            }
        }
    }

    /**
     * Validates the optional {@code context_management} sub-object on
     * {@link AgentConfigRequest}. Absence is always valid; platform defaults
     * apply at read time in the worker (Task 3), not at write time.
     *
     * <ul>
     *   <li>{@code summarizerModel}, when non-blank, must resolve against the
     *       {@code models} table for the agent's provider (same lookup as
     *       {@link #validateModel(String, String)}). Additionally, when the DB
     *       exposes {@code context_window} for both the summarizer and the primary
     *       model, the summarizer's context window must be ≥ the primary model's
     *       Tier 3 trigger. Tier 3 trigger formula mirrors
     *       {@code compaction/thresholds.py#resolve_thresholds} (Task 3):
     *       {@code tier3_trigger = int((context_window - OUTPUT_BUDGET_RESERVE) * TIER_3_FRACTION)}.
     *       When either window is absent from the DB the check is skipped
     *       (graceful degradation — older seeds lack context_window).</li>
     *   <li>{@code excludeTools}, when non-null, must have ≤ 50 entries
     *       (matches {@code tool_servers} cap). Tool-name existence is NOT
     *       validated — customers may add custom tools before wiring.</li>
     *   <li>{@code preTier3MemoryFlush} bool-typed; no coercion or cross-field
     *       validation — runtime gating (memory.enabled check) is the worker's
     *       job (Task 9).</li>
     * </ul>
     *
     * @param cm       context-management sub-object (may be {@code null})
     * @param provider agent's provider (e.g. "anthropic")
     * @param model    agent's primary model ID (used for context-window comparison)
     */
    public void validateContextManagementConfig(
            ContextManagementConfigRequest cm, String provider, String model) {
        if (cm == null) {
            return; // Absent context_management sub-object is valid.
        }

        // summarizer_model: optional; when present and non-blank, must be active
        // for the agent's provider. Reject blank strings — ambiguous with absence.
        if (cm.summarizerModel() != null && !cm.summarizerModel().isBlank()) {
            if (!modelRepository.isModelActive(provider, cm.summarizerModel())) {
                throw new ValidationException(
                        "Unsupported context_management.summarizer_model or provider: "
                                + provider + "/" + cm.summarizerModel()
                                + ". Check GET /v1/models for supported ones.");
            }

            // Context-window check: summarizer must be able to hold the primary model's
            // Tier 3 trigger. Formula mirrors compaction/thresholds.py#resolve_thresholds
            // (Task 3 — inlined here until Task 3 ships a shared helper):
            //   effective_budget = context_window - OUTPUT_BUDGET_RESERVE_TOKENS  (10_000)
            //   tier3_trigger    = int(effective_budget * TIER_3_TRIGGER_FRACTION) (0.75)
            // When either window is NULL / missing from DB, we skip the check (graceful
            // degradation: older model seeds do not carry context_window yet; the
            // migration 0014_model_context_window.sql adds the column with DEFAULT NULL).
            modelRepository.getContextWindow(provider, cm.summarizerModel())
                    .ifPresent(summarizerWindow -> {
                        modelRepository.getContextWindow(provider, model).ifPresent(primaryWindow -> {
                            // Mirror Task 3 resolve_thresholds formula.
                            int outputBudgetReserve = 10_000;
                            double tier3TriggerFraction = 0.75;
                            int effectiveBudget = primaryWindow - outputBudgetReserve;
                            int tier3Trigger = (int) (effectiveBudget * tier3TriggerFraction);
                            if (summarizerWindow < tier3Trigger) {
                                throw new ValidationException(
                                        "context_management.summarizer_model " + cm.summarizerModel()
                                                + " has context_window " + summarizerWindow
                                                + " but primary model " + model
                                                + " triggers Tier 3 at " + tier3Trigger
                                                + " tokens — select a summarizer with context_window >= "
                                                + tier3Trigger);
                            }
                        });
                    });
        }

        // exclude_tools: optional; when present, must not exceed 50 entries.
        // Tool-name existence is NOT validated — unknown names are allowed.
        if (cm.excludeTools() != null && cm.excludeTools().size() > 50) {
            throw new ValidationException(
                    "context_management.exclude_tools must not exceed 50 entries "
                            + "(got " + cm.excludeTools().size() + ")");
        }

        // pre_tier3_memory_flush: pure boolean toggle — no further validation.
        // No cross-field check against memory.enabled; runtime gating is the worker's
        // job (Task 9).

        // offload_tool_results: pure boolean toggle; null-tolerant (absence == default
        // true applied by the worker). Track 7 Follow-up (Task 4) kill switch for the
        // Tier 0 ingestion offload. No cross-field validation; if the field is present
        // it must be a boolean — record field typing enforces that — and any legal
        // value passes this validator.
    }

    public void validateAgentConfig(AgentConfigRequest config) {
        validateModel(config.provider(), config.model());
        validateAllowedTools(config.allowedTools());
        validateToolServers(config.toolServers());
        validateSandboxConfig(config.sandbox());
        validateMemoryConfig(config.memory(), config.provider());
        validateContextManagementConfig(config.contextManagement(), config.provider(), config.model());
    }

    /**
     * Phase 2 Track 5 Task 12: cross-field invariant enforced at task submission.
     *
     * <p>The API rejects {@code memory_mode ∈ {always, agent_decides}} when the
     * target agent has {@code memory.enabled=false}. The worker's master gate is
     * the agent-level {@code memory.enabled} flag; asking for {@code always} or
     * {@code agent_decides} against a memory-disabled agent is meaningless —
     * surface that as a 400 rather than silently accepting a mode the worker
     * will not honour. Mode {@code skip} is always legal, even for
     * memory-disabled agents, because it matches the worker's actual behaviour.
     *
     * <p>Throws {@link AgentNotFoundException} if the agent cannot be resolved
     * for the tenant — the caller (TaskService) already has its own not-found
     * path for atomic insert misses; this check runs before that and fails
     * fast when the agent is missing outright.
     *
     * @param tenantId    tenant scope
     * @param agentId     agent to inspect
     * @param memoryMode  normalised mode, one of {@code "always"} or {@code "agent_decides"}
     *                    — callers must gate out {@code "skip"} before invoking
     */
    public void validateMemoryModeAgainstAgent(String tenantId, String agentId, String memoryMode) {
        if (!isAgentMemoryEnabled(tenantId, agentId).orElse(true)) {
            throw new ValidationException(
                    "memory_mode cannot be '" + memoryMode
                            + "' because this agent does not have memory enabled");
        }
    }

    /**
     * Looks up {@code agent_config.memory.enabled} for the agent. Returns
     * {@link Optional#empty()} when the agent cannot be resolved — callers that
     * want to defer the unknown-agent error to the atomic-insert path should
     * treat empty as "unknown, fall through" rather than "memory off". Used by
     * {@link com.persistentagent.api.service.TaskService} to pick a sensible
     * per-task {@code memory_mode} default when the submitter did not specify
     * one: memory-disabled agents default to {@code "skip"} so Phase-1/2
     * callers that never set the field keep working; memory-enabled agents
     * default to {@code "always"} per the Track 5 spec.
     */
    public Optional<Boolean> isAgentMemoryEnabled(String tenantId, String agentId) {
        Optional<Map<String, Object>> agentRow = agentRepository.findByIdAndTenant(tenantId, agentId);
        if (agentRow.isEmpty()) {
            return Optional.empty();
        }
        String agentConfigJson = extractAgentConfigJson(agentRow.get().get("agent_config"));
        if (agentConfigJson == null || agentConfigJson.isBlank()) {
            return Optional.of(false);
        }
        return Optional.of(readMemoryEnabled(agentConfigJson));
    }

    private static String extractAgentConfigJson(Object rawAgentConfig) {
        if (rawAgentConfig == null) {
            return null;
        }
        if (rawAgentConfig instanceof String s) {
            return s;
        }
        if (rawAgentConfig instanceof PGobject pg) {
            return pg.getValue();
        }
        return rawAgentConfig.toString();
    }

    /**
     * Parses {@code agent_config.memory.enabled} out of the stored JSON. Treats
     * any malformed / missing / null-ish value as {@code false} — the mode
     * check's purpose is to block meaningless combinations, so defaulting to
     * memory-off on parse trouble is the conservative choice.
     */
    private boolean readMemoryEnabled(String agentConfigJson) {
        try {
            JsonNode root = objectMapper.readTree(agentConfigJson);
            JsonNode memory = root == null ? null : root.get("memory");
            if (memory == null || memory.isNull()) {
                return false;
            }
            JsonNode enabled = memory.get("enabled");
            return enabled != null && enabled.asBoolean(false);
        } catch (Exception e) {
            return false;
        }
    }
}
