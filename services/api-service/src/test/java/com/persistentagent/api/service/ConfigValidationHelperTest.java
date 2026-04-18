package com.persistentagent.api.service;

import com.persistentagent.api.exception.ValidationException;
import com.persistentagent.api.model.request.MemoryConfigRequest;
import com.persistentagent.api.repository.ModelRepository;
import com.persistentagent.api.repository.ToolServerRepository;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;

import java.util.HashMap;
import java.util.List;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.ArgumentMatchers.*;
import static org.mockito.Mockito.*;

@ExtendWith(MockitoExtension.class)
class ConfigValidationHelperTest {

    @Mock
    private ModelRepository modelRepository;

    @Mock
    private ToolServerRepository toolServerRepository;

    private ConfigValidationHelper helper;

    @BeforeEach
    void setUp() {
        helper = new ConfigValidationHelper(modelRepository, toolServerRepository, false);
    }

    // --- validateToolServers tests ---

    @Test
    void testValidateToolServers_null_ok() {
        // null tool_servers should pass without any repository calls
        assertDoesNotThrow(() -> helper.validateToolServers(null));
        verifyNoInteractions(toolServerRepository);
    }

    @Test
    void testValidateToolServers_empty_ok() {
        // empty list should pass without any repository calls
        assertDoesNotThrow(() -> helper.validateToolServers(List.of()));
        verifyNoInteractions(toolServerRepository);
    }

    @Test
    void testValidateToolServers_validNames_ok() {
        // valid, existing, active servers should pass
        Map<String, Object> row = new HashMap<>();
        row.put("name", "jira-tools");
        row.put("status", "active");

        when(toolServerRepository.findByTenantAndNames(eq("default"), eq(List.of("jira-tools"))))
                .thenReturn(List.of(row));

        assertDoesNotThrow(() -> helper.validateToolServers(List.of("jira-tools")));
        verify(toolServerRepository).findByTenantAndNames("default", List.of("jira-tools"));
    }

    @Test
    void testValidateToolServers_multipleValidNames_ok() {
        // multiple valid, existing, active servers should pass
        Map<String, Object> row1 = new HashMap<>();
        row1.put("name", "jira-tools");
        row1.put("status", "active");

        Map<String, Object> row2 = new HashMap<>();
        row2.put("name", "slack-tools");
        row2.put("status", "active");

        when(toolServerRepository.findByTenantAndNames(eq("default"), anyList()))
                .thenReturn(List.of(row1, row2));

        assertDoesNotThrow(() -> helper.validateToolServers(List.of("jira-tools", "slack-tools")));
    }

    @Test
    void testValidateToolServers_duplicateName_throws() {
        // duplicate names should throw ValidationException before hitting the repository
        ValidationException ex = assertThrows(ValidationException.class,
                () -> helper.validateToolServers(List.of("jira-tools", "jira-tools")));
        assertTrue(ex.getMessage().contains("Duplicate tool server name: jira-tools"));
        verifyNoInteractions(toolServerRepository);
    }

    @Test
    void testValidateToolServers_invalidNameFormat_throws() {
        // uppercase name does not match the required pattern
        ValidationException ex = assertThrows(ValidationException.class,
                () -> helper.validateToolServers(List.of("UPPERCASE")));
        assertTrue(ex.getMessage().contains("Invalid tool server name: UPPERCASE"));
        verifyNoInteractions(toolServerRepository);
    }

    @Test
    void testValidateToolServers_nameWithSpaces_throws() {
        // name with spaces does not match the required pattern
        ValidationException ex = assertThrows(ValidationException.class,
                () -> helper.validateToolServers(List.of("invalid name")));
        assertTrue(ex.getMessage().contains("Invalid tool server name: invalid name"));
        verifyNoInteractions(toolServerRepository);
    }

    @Test
    void testValidateToolServers_serverNotFound_throws() {
        // referencing a non-existent server should throw ValidationException
        when(toolServerRepository.findByTenantAndNames(eq("default"), anyList()))
                .thenReturn(List.of()); // empty result — server not found

        ValidationException ex = assertThrows(ValidationException.class,
                () -> helper.validateToolServers(List.of("nonexistent-server")));
        assertTrue(ex.getMessage().contains("Tool server not found: nonexistent-server"));
    }

    @Test
    void testValidateToolServers_serverDisabled_throws() {
        // referencing a disabled server should throw ValidationException
        Map<String, Object> row = new HashMap<>();
        row.put("name", "disabled-server");
        row.put("status", "disabled");

        when(toolServerRepository.findByTenantAndNames(eq("default"), anyList()))
                .thenReturn(List.of(row));

        ValidationException ex = assertThrows(ValidationException.class,
                () -> helper.validateToolServers(List.of("disabled-server")));
        assertTrue(ex.getMessage().contains("Tool server 'disabled-server' is disabled"));
    }

    // --- validateMemoryConfig tests ---

    @Test
    void testValidateMemoryConfig_null_ok() {
        // Absent memory sub-object is always valid.
        assertDoesNotThrow(() -> helper.validateMemoryConfig(null, "openai"));
        verifyNoInteractions(modelRepository);
    }

    @Test
    void testValidateMemoryConfig_allFieldsNull_ok() {
        // All three fields null (enabled flag alone may be present or absent) — no checks.
        MemoryConfigRequest memory = new MemoryConfigRequest(null, null, null);
        assertDoesNotThrow(() -> helper.validateMemoryConfig(memory, "openai"));
        verifyNoInteractions(modelRepository);
    }

    @Test
    void testValidateMemoryConfig_enabledTrueOnly_ok() {
        // Only enabled=true with no other fields — no model lookup, no bounds check.
        MemoryConfigRequest memory = new MemoryConfigRequest(true, null, null);
        assertDoesNotThrow(() -> helper.validateMemoryConfig(memory, "openai"));
        verifyNoInteractions(modelRepository);
    }

    @Test
    void testValidateMemoryConfig_enabledFalse_ok() {
        // Explicit enabled=false — still no lookup even if other fields set.
        MemoryConfigRequest memory = new MemoryConfigRequest(false, null, null);
        assertDoesNotThrow(() -> helper.validateMemoryConfig(memory, "openai"));
        verifyNoInteractions(modelRepository);
    }

    @Test
    void testValidateMemoryConfig_summarizerModelValid_ok() {
        // Known-active summarizer model resolves for the agent's provider.
        when(modelRepository.isModelActive("anthropic", "claude-haiku-4-5"))
                .thenReturn(true);

        MemoryConfigRequest memory = new MemoryConfigRequest(true, "claude-haiku-4-5", null);
        assertDoesNotThrow(() -> helper.validateMemoryConfig(memory, "anthropic"));
        verify(modelRepository).isModelActive("anthropic", "claude-haiku-4-5");
    }

    @Test
    void testValidateMemoryConfig_summarizerModelEmptyString_skipped() {
        // Empty or whitespace-only summarizer_model behaves like absence — no lookup.
        MemoryConfigRequest memory = new MemoryConfigRequest(true, "   ", null);
        assertDoesNotThrow(() -> helper.validateMemoryConfig(memory, "openai"));
        verifyNoInteractions(modelRepository);
    }

    @Test
    void testValidateMemoryConfig_summarizerModelUnknown_throws() {
        // Unknown summarizer_model — rejected with the same shape as validateModel.
        when(modelRepository.isModelActive("openai", "nonexistent-model"))
                .thenReturn(false);

        MemoryConfigRequest memory = new MemoryConfigRequest(true, "nonexistent-model", null);
        ValidationException ex = assertThrows(ValidationException.class,
                () -> helper.validateMemoryConfig(memory, "openai"));
        assertTrue(ex.getMessage().contains("nonexistent-model"),
                "error message should name the offending model");
        assertTrue(ex.getMessage().contains("openai"),
                "error message should name the provider");
    }

    @Test
    void testValidateMemoryConfig_summarizerModelDisabled_throws() {
        // Model exists but is inactive — same branch as unknown.
        when(modelRepository.isModelActive("openai", "retired-model"))
                .thenReturn(false);

        MemoryConfigRequest memory = new MemoryConfigRequest(true, "retired-model", null);
        assertThrows(ValidationException.class,
                () -> helper.validateMemoryConfig(memory, "openai"));
    }

    @Test
    void testValidateMemoryConfig_maxEntriesValid_ok() {
        MemoryConfigRequest memory = new MemoryConfigRequest(true, null, 5000);
        assertDoesNotThrow(() -> helper.validateMemoryConfig(memory, "openai"));
        verifyNoInteractions(modelRepository);
    }

    @Test
    void testValidateMemoryConfig_maxEntriesAtLowerBound_ok() {
        // 100 is the inclusive lower bound.
        MemoryConfigRequest memory = new MemoryConfigRequest(true, null, 100);
        assertDoesNotThrow(() -> helper.validateMemoryConfig(memory, "openai"));
    }

    @Test
    void testValidateMemoryConfig_maxEntriesAtUpperBound_ok() {
        // 100_000 is the inclusive upper bound.
        MemoryConfigRequest memory = new MemoryConfigRequest(true, null, 100_000);
        assertDoesNotThrow(() -> helper.validateMemoryConfig(memory, "openai"));
    }

    @Test
    void testValidateMemoryConfig_maxEntriesBelowMin_throws() {
        MemoryConfigRequest memory = new MemoryConfigRequest(true, null, 10);
        ValidationException ex = assertThrows(ValidationException.class,
                () -> helper.validateMemoryConfig(memory, "openai"));
        assertTrue(ex.getMessage().contains("100"),
                "error message should name lower bound: " + ex.getMessage());
        assertTrue(ex.getMessage().contains("100000"),
                "error message should name upper bound: " + ex.getMessage());
    }

    @Test
    void testValidateMemoryConfig_maxEntriesJustBelowMin_throws() {
        MemoryConfigRequest memory = new MemoryConfigRequest(true, null, 99);
        assertThrows(ValidationException.class,
                () -> helper.validateMemoryConfig(memory, "openai"));
    }

    @Test
    void testValidateMemoryConfig_maxEntriesAboveMax_throws() {
        MemoryConfigRequest memory = new MemoryConfigRequest(true, null, 500_000);
        ValidationException ex = assertThrows(ValidationException.class,
                () -> helper.validateMemoryConfig(memory, "openai"));
        assertTrue(ex.getMessage().contains("100"));
        assertTrue(ex.getMessage().contains("100000"));
    }

    @Test
    void testValidateMemoryConfig_maxEntriesJustAboveMax_throws() {
        MemoryConfigRequest memory = new MemoryConfigRequest(true, null, 100_001);
        assertThrows(ValidationException.class,
                () -> helper.validateMemoryConfig(memory, "openai"));
    }

    @Test
    void testValidateMemoryConfig_maxEntriesZero_throws() {
        MemoryConfigRequest memory = new MemoryConfigRequest(true, null, 0);
        assertThrows(ValidationException.class,
                () -> helper.validateMemoryConfig(memory, "openai"));
    }

    @Test
    void testValidateMemoryConfig_maxEntriesNegative_throws() {
        MemoryConfigRequest memory = new MemoryConfigRequest(true, null, -1);
        assertThrows(ValidationException.class,
                () -> helper.validateMemoryConfig(memory, "openai"));
    }

    @Test
    void testValidateMemoryConfig_allFieldsValid_ok() {
        // Combination of all fields present and valid.
        when(modelRepository.isModelActive("anthropic", "claude-haiku-4-5"))
                .thenReturn(true);

        MemoryConfigRequest memory = new MemoryConfigRequest(true, "claude-haiku-4-5", 25_000);
        assertDoesNotThrow(() -> helper.validateMemoryConfig(memory, "anthropic"));
    }
}
