package com.persistentagent.api.service;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.persistentagent.api.exception.ValidationException;
import com.persistentagent.api.model.request.SandboxConfigRequest;
import com.persistentagent.api.repository.AgentRepository;
import com.persistentagent.api.repository.ModelRepository;
import com.persistentagent.api.repository.ToolServerRepository;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;

import static org.junit.jupiter.api.Assertions.*;

@ExtendWith(MockitoExtension.class)
class SandboxConfigValidationTest {

    @Mock
    private ModelRepository modelRepository;

    @Mock
    private ToolServerRepository toolServerRepository;

    @Mock
    private AgentRepository agentRepository;

    private ConfigValidationHelper helper;

    @BeforeEach
    void setUp() {
        helper = new ConfigValidationHelper(
                modelRepository, toolServerRepository, agentRepository, new ObjectMapper(), false);
    }

    @Test
    void validateSandboxConfig_nullConfig_noError() {
        assertDoesNotThrow(() -> helper.validateSandboxConfig(null));
    }

    @Test
    void validateSandboxConfig_disabledExplicitly_noError() {
        SandboxConfigRequest config = new SandboxConfigRequest(false, null, null, null, null);
        assertDoesNotThrow(() -> helper.validateSandboxConfig(config));
    }

    @Test
    void validateSandboxConfig_enabledNullIsFalse_noError() {
        SandboxConfigRequest config = new SandboxConfigRequest(null, null, null, null, null);
        assertDoesNotThrow(() -> helper.validateSandboxConfig(config));
    }

    @Test
    void validateSandboxConfig_enabledWithValidConfig_noError() {
        SandboxConfigRequest config = new SandboxConfigRequest(true, "python-3.11", 2, 2048, 3600);
        assertDoesNotThrow(() -> helper.validateSandboxConfig(config));
    }

    @Test
    void validateSandboxConfig_enabledWithDefaults_noError() {
        SandboxConfigRequest config = new SandboxConfigRequest(true, "python-3.11", null, null, null);
        assertDoesNotThrow(() -> helper.validateSandboxConfig(config));
    }

    @Test
    void validateSandboxConfig_enabledMissingTemplate_throwsValidation() {
        SandboxConfigRequest config = new SandboxConfigRequest(true, null, 2, 2048, 3600);
        assertThrows(ValidationException.class, () -> helper.validateSandboxConfig(config));
    }

    @Test
    void validateSandboxConfig_enabledBlankTemplate_throwsValidation() {
        SandboxConfigRequest config = new SandboxConfigRequest(true, "  ", 2, 2048, 3600);
        assertThrows(ValidationException.class, () -> helper.validateSandboxConfig(config));
    }

    @Test
    void validateSandboxConfig_vcpuBelowMin_throwsValidation() {
        SandboxConfigRequest config = new SandboxConfigRequest(true, "python-3.11", 0, 2048, 3600);
        assertThrows(ValidationException.class, () -> helper.validateSandboxConfig(config));
    }

    @Test
    void validateSandboxConfig_vcpuAboveMax_throwsValidation() {
        SandboxConfigRequest config = new SandboxConfigRequest(true, "python-3.11", 9, 2048, 3600);
        assertThrows(ValidationException.class, () -> helper.validateSandboxConfig(config));
    }

    @Test
    void validateSandboxConfig_memoryBelowMin_throwsValidation() {
        SandboxConfigRequest config = new SandboxConfigRequest(true, "python-3.11", 2, 256, 3600);
        assertThrows(ValidationException.class, () -> helper.validateSandboxConfig(config));
    }

    @Test
    void validateSandboxConfig_memoryAboveMax_throwsValidation() {
        SandboxConfigRequest config = new SandboxConfigRequest(true, "python-3.11", 2, 16384, 3600);
        assertThrows(ValidationException.class, () -> helper.validateSandboxConfig(config));
    }

    @Test
    void validateSandboxConfig_timeoutBelowMin_throwsValidation() {
        SandboxConfigRequest config = new SandboxConfigRequest(true, "python-3.11", 2, 2048, 30);
        assertThrows(ValidationException.class, () -> helper.validateSandboxConfig(config));
    }

    @Test
    void validateSandboxConfig_timeoutAboveMax_throwsValidation() {
        SandboxConfigRequest config = new SandboxConfigRequest(true, "python-3.11", 2, 2048, 100000);
        assertThrows(ValidationException.class, () -> helper.validateSandboxConfig(config));
    }

    @Test
    void validateSandboxConfig_vcpuBoundaryMin_noError() {
        SandboxConfigRequest config = new SandboxConfigRequest(true, "python-3.11", 1, 2048, 3600);
        assertDoesNotThrow(() -> helper.validateSandboxConfig(config));
    }

    @Test
    void validateSandboxConfig_vcpuBoundaryMax_noError() {
        SandboxConfigRequest config = new SandboxConfigRequest(true, "python-3.11", 8, 2048, 3600);
        assertDoesNotThrow(() -> helper.validateSandboxConfig(config));
    }

    @Test
    void validateSandboxConfig_memoryBoundaryMin_noError() {
        SandboxConfigRequest config = new SandboxConfigRequest(true, "python-3.11", 2, 512, 3600);
        assertDoesNotThrow(() -> helper.validateSandboxConfig(config));
    }

    @Test
    void validateSandboxConfig_memoryBoundaryMax_noError() {
        SandboxConfigRequest config = new SandboxConfigRequest(true, "python-3.11", 2, 8192, 3600);
        assertDoesNotThrow(() -> helper.validateSandboxConfig(config));
    }

    @Test
    void validateSandboxConfig_timeoutBoundaryMin_noError() {
        SandboxConfigRequest config = new SandboxConfigRequest(true, "python-3.11", 2, 2048, 60);
        assertDoesNotThrow(() -> helper.validateSandboxConfig(config));
    }

    @Test
    void validateSandboxConfig_timeoutBoundaryMax_noError() {
        SandboxConfigRequest config = new SandboxConfigRequest(true, "python-3.11", 2, 2048, 86400);
        assertDoesNotThrow(() -> helper.validateSandboxConfig(config));
    }
}
