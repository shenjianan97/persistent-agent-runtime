package com.persistentagent.api.service;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.persistentagent.api.exception.ValidationException;
import com.persistentagent.api.model.request.ContextManagementConfigRequest;
import com.persistentagent.api.model.request.MemoryConfigRequest;
import com.persistentagent.api.repository.AgentRepository;
import com.persistentagent.api.repository.ModelRepository;
import com.persistentagent.api.repository.ToolServerRepository;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.postgresql.util.PGobject;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;

import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.Optional;

import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.ArgumentMatchers.*;
import static org.mockito.Mockito.*;

@ExtendWith(MockitoExtension.class)
class ConfigValidationHelperTest {

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

    // --- validateMemoryModeAgainstAgent tests ---

    @Test
    void testValidateMemoryModeAgainstAgent_pgobjectConfig_memoryDisabled_throws() throws Exception {
        // Regression: the JDBC driver hands jsonb columns back as PGobject, not String.
        // An earlier version of this helper cast directly to String and produced a
        // 500 for every task submission against the live DB.
        PGobject pg = new PGobject();
        pg.setType("jsonb");
        pg.setValue("{\"memory\":{\"enabled\":false}}");

        when(agentRepository.findByIdAndTenant("tenant", "agent-x"))
                .thenReturn(Optional.of(Map.of("agent_config", pg)));

        assertThrows(ValidationException.class,
                () -> helper.validateMemoryModeAgainstAgent("tenant", "agent-x", "always"));
    }

    @Test
    void testValidateMemoryModeAgainstAgent_pgobjectConfig_memoryEnabled_ok() throws Exception {
        PGobject pg = new PGobject();
        pg.setType("jsonb");
        pg.setValue("{\"memory\":{\"enabled\":true}}");

        when(agentRepository.findByIdAndTenant("tenant", "agent-x"))
                .thenReturn(Optional.of(Map.of("agent_config", pg)));

        assertDoesNotThrow(
                () -> helper.validateMemoryModeAgainstAgent("tenant", "agent-x", "agent_decides"));
    }

    @Test
    void testValidateMemoryModeAgainstAgent_stringConfig_memoryDisabled_throws() {
        when(agentRepository.findByIdAndTenant("tenant", "agent-x"))
                .thenReturn(Optional.of(Map.of("agent_config", "{\"memory\":{\"enabled\":false}}")));

        assertThrows(ValidationException.class,
                () -> helper.validateMemoryModeAgainstAgent("tenant", "agent-x", "always"));
    }

    // --- validateContextManagementConfig tests ---

    @Test
    void testValidateContextManagementConfig_null_ok() {
        // Absent context_management sub-object is always valid.
        assertDoesNotThrow(() -> helper.validateContextManagementConfig(null, "openai", "gpt-4o"));
        verifyNoInteractions(modelRepository);
    }

    @Test
    void testValidateContextManagementConfig_allFieldsNull_ok() {
        // All fields null — no checks needed.
        ContextManagementConfigRequest cm = new ContextManagementConfigRequest(null, null, null);
        assertDoesNotThrow(() -> helper.validateContextManagementConfig(cm, "openai", "gpt-4o"));
        verifyNoInteractions(modelRepository);
    }

    @Test
    void testValidateContextManagementConfig_summarizerModelValid_ok() {
        // Known-active summarizer model resolves for the agent's provider.
        when(modelRepository.isModelActive("anthropic", "claude-haiku-4-5")).thenReturn(true);
        // No context window available (returns empty) — check is skipped.
        when(modelRepository.getContextWindow("anthropic", "claude-haiku-4-5")).thenReturn(Optional.empty());

        ContextManagementConfigRequest cm = new ContextManagementConfigRequest("claude-haiku-4-5", null, null);
        assertDoesNotThrow(() -> helper.validateContextManagementConfig(cm, "anthropic", "claude-sonnet-4-6"));
        verify(modelRepository).isModelActive("anthropic", "claude-haiku-4-5");
    }

    @Test
    void testValidateContextManagementConfig_summarizerModelBlankString_skipped() {
        // Empty or whitespace-only summarizer_model behaves like absence — no lookup.
        ContextManagementConfigRequest cm = new ContextManagementConfigRequest("   ", null, null);
        assertDoesNotThrow(() -> helper.validateContextManagementConfig(cm, "openai", "gpt-4o"));
        verifyNoInteractions(modelRepository);
    }

    @Test
    void testValidateContextManagementConfig_summarizerModelUnknown_throws() {
        // Unknown summarizer_model — rejected with the same shape as validateModel.
        when(modelRepository.isModelActive("openai", "nonexistent-model")).thenReturn(false);

        ContextManagementConfigRequest cm = new ContextManagementConfigRequest("nonexistent-model", null, null);
        ValidationException ex = assertThrows(ValidationException.class,
                () -> helper.validateContextManagementConfig(cm, "openai", "gpt-4o"));
        assertTrue(ex.getMessage().contains("nonexistent-model"),
                "error message should name the offending model: " + ex.getMessage());
        assertTrue(ex.getMessage().contains("openai"),
                "error message should name the provider: " + ex.getMessage());
    }

    @Test
    void testValidateContextManagementConfig_summarizerModelDisabled_throws() {
        // Model exists but is inactive — same 400 path.
        when(modelRepository.isModelActive("openai", "retired-model")).thenReturn(false);

        ContextManagementConfigRequest cm = new ContextManagementConfigRequest("retired-model", null, null);
        assertThrows(ValidationException.class,
                () -> helper.validateContextManagementConfig(cm, "openai", "gpt-4o"));
    }

    @Test
    void testValidateContextManagementConfig_contextWindowCheck_summarizerTooSmall_throws() {
        // Summarizer has 32K context window; primary model triggers Tier 3 at ~142K.
        // The summarizer context_window (32K) < primary tier3_trigger (~142K) → 400.
        when(modelRepository.isModelActive("anthropic", "small-model")).thenReturn(true);
        when(modelRepository.getContextWindow("anthropic", "small-model"))
                .thenReturn(Optional.of(32_000));
        when(modelRepository.getContextWindow("anthropic", "claude-sonnet-4-6"))
                .thenReturn(Optional.of(200_000));

        ContextManagementConfigRequest cm = new ContextManagementConfigRequest("small-model", null, null);
        ValidationException ex = assertThrows(ValidationException.class,
                () -> helper.validateContextManagementConfig(cm, "anthropic", "claude-sonnet-4-6"));
        assertTrue(ex.getMessage().contains("small-model"),
                "error message should name the offending summarizer: " + ex.getMessage());
        assertTrue(ex.getMessage().contains("context_window"),
                "error message should mention context_window: " + ex.getMessage());
    }

    @Test
    void testValidateContextManagementConfig_contextWindowCheck_summarizerSufficient_ok() {
        // Summarizer has 200K context window; primary triggers Tier 3 at ~142K.
        // The summarizer context_window (200K) >= primary tier3_trigger (~142K) → accepted.
        when(modelRepository.isModelActive("anthropic", "claude-haiku-4-5")).thenReturn(true);
        when(modelRepository.getContextWindow("anthropic", "claude-haiku-4-5"))
                .thenReturn(Optional.of(200_000));
        when(modelRepository.getContextWindow("anthropic", "claude-sonnet-4-6"))
                .thenReturn(Optional.of(200_000));

        ContextManagementConfigRequest cm = new ContextManagementConfigRequest("claude-haiku-4-5", null, null);
        assertDoesNotThrow(() -> helper.validateContextManagementConfig(cm, "anthropic", "claude-sonnet-4-6"));
    }

    @Test
    void testValidateContextManagementConfig_contextWindowCheck_primaryWindowUnknown_skipsCheck() {
        // Primary model context window not in DB — check is skipped (graceful degradation).
        when(modelRepository.isModelActive("anthropic", "claude-haiku-4-5")).thenReturn(true);
        when(modelRepository.getContextWindow("anthropic", "claude-haiku-4-5"))
                .thenReturn(Optional.of(32_000));
        when(modelRepository.getContextWindow("anthropic", "claude-sonnet-4-6"))
                .thenReturn(Optional.empty()); // primary window unknown

        ContextManagementConfigRequest cm = new ContextManagementConfigRequest("claude-haiku-4-5", null, null);
        // Skips the check when primary window is unknown — no 400.
        assertDoesNotThrow(() -> helper.validateContextManagementConfig(cm, "anthropic", "claude-sonnet-4-6"));
    }

    @Test
    void testValidateContextManagementConfig_excludeTools_exactly50_ok() {
        // 50 entries is at the cap — must accept.
        List<String> tools = new java.util.ArrayList<>();
        for (int i = 0; i < 50; i++) {
            tools.add("tool_" + i);
        }
        ContextManagementConfigRequest cm = new ContextManagementConfigRequest(null, tools, null);
        assertDoesNotThrow(() -> helper.validateContextManagementConfig(cm, "openai", "gpt-4o"));
    }

    @Test
    void testValidateContextManagementConfig_excludeTools_51entries_throws() {
        // 51 entries exceeds the 50-entry cap — must reject with a message naming the cap.
        List<String> tools = new java.util.ArrayList<>();
        for (int i = 0; i < 51; i++) {
            tools.add("tool_" + i);
        }
        ContextManagementConfigRequest cm = new ContextManagementConfigRequest(null, tools, null);
        ValidationException ex = assertThrows(ValidationException.class,
                () -> helper.validateContextManagementConfig(cm, "openai", "gpt-4o"));
        assertTrue(ex.getMessage().contains("50"),
                "error message must name the 50-entry cap: " + ex.getMessage());
    }

    @Test
    void testValidateContextManagementConfig_excludeTools_unknownToolNames_ok() {
        // Unknown tool names are allowed — customers can add custom tools before wiring.
        List<String> tools = List.of("memory_note", "unknown_tool_xyz");
        ContextManagementConfigRequest cm = new ContextManagementConfigRequest(null, tools, null);
        assertDoesNotThrow(() -> helper.validateContextManagementConfig(cm, "openai", "gpt-4o"));
    }

    @Test
    void testValidateContextManagementConfig_excludeTools_empty_ok() {
        // Empty exclude_tools list is valid.
        ContextManagementConfigRequest cm = new ContextManagementConfigRequest(null, List.of(), null);
        assertDoesNotThrow(() -> helper.validateContextManagementConfig(cm, "openai", "gpt-4o"));
    }

    @Test
    void testValidateContextManagementConfig_preTier3MemoryFlush_true_ok() {
        // pre_tier3_memory_flush=true is valid regardless of memory.enabled state.
        ContextManagementConfigRequest cm = new ContextManagementConfigRequest(null, null, true);
        assertDoesNotThrow(() -> helper.validateContextManagementConfig(cm, "openai", "gpt-4o"));
        verifyNoInteractions(modelRepository);
    }

    @Test
    void testValidateContextManagementConfig_preTier3MemoryFlush_false_ok() {
        // pre_tier3_memory_flush=false is also valid.
        ContextManagementConfigRequest cm = new ContextManagementConfigRequest(null, null, false);
        assertDoesNotThrow(() -> helper.validateContextManagementConfig(cm, "openai", "gpt-4o"));
        verifyNoInteractions(modelRepository);
    }

    @Test
    void testValidateContextManagementConfig_allFieldsValid_ok() {
        // Combination of all fields present and valid — accepted.
        when(modelRepository.isModelActive("anthropic", "claude-haiku-4-5")).thenReturn(true);
        when(modelRepository.getContextWindow("anthropic", "claude-haiku-4-5"))
                .thenReturn(Optional.of(200_000));
        when(modelRepository.getContextWindow("anthropic", "claude-sonnet-4-6"))
                .thenReturn(Optional.of(200_000));

        List<String> tools = List.of("memory_note", "custom_tool");
        ContextManagementConfigRequest cm = new ContextManagementConfigRequest(
                "claude-haiku-4-5", tools, true);
        assertDoesNotThrow(() -> helper.validateContextManagementConfig(cm, "anthropic", "claude-sonnet-4-6"));
    }

    @Test
    void testValidateAgentConfig_withContextManagement_invokesNestedValidation() {
        // validateAgentConfig must call validateContextManagementConfig when the sub-object is present.
        // We verify this by checking that an invalid summarizer_model produces a 400 via the top-level call.
        when(modelRepository.isModelActive("openai", "gpt-4o")).thenReturn(true);
        when(modelRepository.isModelActive("openai", "bad-cm-model")).thenReturn(false);

        ContextManagementConfigRequest cm = new ContextManagementConfigRequest("bad-cm-model", null, null);
        com.persistentagent.api.model.request.AgentConfigRequest config =
                new com.persistentagent.api.model.request.AgentConfigRequest(
                        "prompt", "openai", "gpt-4o", 0.7, List.of(), null, null, null, cm);

        assertThrows(ValidationException.class, () -> helper.validateAgentConfig(config),
                "validateAgentConfig must propagate context_management validation errors");
    }
}
