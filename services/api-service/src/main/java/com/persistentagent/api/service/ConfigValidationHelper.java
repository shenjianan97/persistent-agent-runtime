package com.persistentagent.api.service;

import com.persistentagent.api.config.ValidationConstants;
import com.persistentagent.api.exception.ValidationException;
import com.persistentagent.api.model.request.AgentConfigRequest;
import com.persistentagent.api.repository.ModelRepository;
import com.persistentagent.api.repository.ToolServerRepository;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Component;

import java.util.HashSet;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;

@Component
public class ConfigValidationHelper {

    private final ModelRepository modelRepository;
    private final ToolServerRepository toolServerRepository;
    private final Set<String> allowedTools;

    public ConfigValidationHelper(
            ModelRepository modelRepository,
            ToolServerRepository toolServerRepository,
            @Value("${app.dev-task-controls.enabled:false}") boolean devTaskControlsEnabled) {
        this.modelRepository = modelRepository;
        this.toolServerRepository = toolServerRepository;

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
            if (!name.matches(ValidationConstants.TOOL_SERVER_NAME_PATTERN)) {
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

    public void validateAgentConfig(AgentConfigRequest config) {
        validateModel(config.provider(), config.model());
        validateAllowedTools(config.allowedTools());
        validateToolServers(config.toolServers());
    }
}
