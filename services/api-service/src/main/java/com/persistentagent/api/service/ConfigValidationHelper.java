package com.persistentagent.api.service;

import com.persistentagent.api.config.ValidationConstants;
import com.persistentagent.api.exception.ValidationException;
import com.persistentagent.api.model.request.AgentConfigRequest;
import com.persistentagent.api.repository.ModelRepository;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Component;

import java.util.LinkedHashSet;
import java.util.List;
import java.util.Set;

@Component
public class ConfigValidationHelper {

    private final ModelRepository modelRepository;
    private final Set<String> allowedTools;

    public ConfigValidationHelper(
            ModelRepository modelRepository,
            @Value("${app.dev-task-controls.enabled:false}") boolean devTaskControlsEnabled) {
        this.modelRepository = modelRepository;

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

    public void validateAgentConfig(AgentConfigRequest config) {
        validateModel(config.provider(), config.model());
        validateAllowedTools(config.allowedTools());
    }
}
