package com.persistentagent.api.service;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.SerializationFeature;
import com.fasterxml.jackson.datatype.jsr310.JavaTimeModule;
import com.persistentagent.api.exception.AgentNotFoundException;
import com.persistentagent.api.exception.ValidationException;
import com.persistentagent.api.model.request.AgentConfigRequest;
import com.persistentagent.api.model.request.AgentCreateRequest;
import com.persistentagent.api.model.request.AgentUpdateRequest;
import com.persistentagent.api.model.request.ContextManagementConfigRequest;
import com.persistentagent.api.model.request.MemoryConfigRequest;
import com.persistentagent.api.model.response.AgentResponse;
import com.persistentagent.api.model.response.AgentSummaryResponse;
import com.persistentagent.api.repository.AgentRepository;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;
import org.mockito.ArgumentCaptor;

import java.sql.Timestamp;
import java.time.Instant;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Optional;

import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.ArgumentMatchers.*;
import static org.mockito.Mockito.*;

@ExtendWith(MockitoExtension.class)
class AgentServiceTest {

    @Mock
    private AgentRepository agentRepository;

    @Mock
    private ConfigValidationHelper configValidationHelper;

    private AgentService agentService;
    private ObjectMapper objectMapper;

    private static final String TENANT_ID = "default";

    @BeforeEach
    void setUp() {
        objectMapper = new ObjectMapper();
        objectMapper.registerModule(new JavaTimeModule());
        objectMapper.disable(SerializationFeature.WRITE_DATES_AS_TIMESTAMPS);
        agentService = new AgentService(agentRepository, configValidationHelper, objectMapper, true);
    }

    // --- createAgent tests ---

    @Test
    void createAgent_success() {
        AgentConfigRequest config = new AgentConfigRequest(
                "You are a helpful assistant.", "openai", "gpt-4o", 0.7, List.of("web_search"), null, null, null, null);
        AgentCreateRequest request = new AgentCreateRequest("Test Agent", config, null, null, null);

        doNothing().when(configValidationHelper).validateAgentConfig(any());

        Timestamp now = Timestamp.from(Instant.now());
        Map<String, Object> repoResult = new LinkedHashMap<>();
        repoResult.put("created_at", now);
        repoResult.put("updated_at", now);
        when(agentRepository.insert(eq(TENANT_ID), anyString(), eq("Test Agent"), anyString(),
                eq(5), eq(500000L), eq(5000000L)))
                .thenReturn(repoResult);

        AgentResponse response = agentService.createAgent(request);

        assertNotNull(response);
        assertNotNull(response.agentId()); // UUID auto-generated
        assertEquals("Test Agent", response.displayName());
        assertEquals("active", response.status());
        assertEquals(5, response.maxConcurrentTasks());
        assertEquals(500000L, response.budgetMaxPerTask());
        assertEquals(5000000L, response.budgetMaxPerHour());
        assertNotNull(response.createdAt());
        assertNotNull(response.updatedAt());
        verify(agentRepository).insertRuntimeState(eq(TENANT_ID), anyString());
    }

    @Test
    void createAgent_withCustomBudgetFields_success() {
        AgentConfigRequest config = new AgentConfigRequest(
                "You are a helpful assistant.", "openai", "gpt-4o", 0.7, List.of("web_search"), null, null, null, null);
        AgentCreateRequest request = new AgentCreateRequest("Test Agent", config, 10, 1000000L, 10000000L);

        doNothing().when(configValidationHelper).validateAgentConfig(any());

        Timestamp now = Timestamp.from(Instant.now());
        Map<String, Object> repoResult = new LinkedHashMap<>();
        repoResult.put("created_at", now);
        repoResult.put("updated_at", now);
        when(agentRepository.insert(eq(TENANT_ID), anyString(), eq("Test Agent"), anyString(),
                eq(10), eq(1000000L), eq(10000000L)))
                .thenReturn(repoResult);

        AgentResponse response = agentService.createAgent(request);

        assertNotNull(response);
        assertEquals(10, response.maxConcurrentTasks());
        assertEquals(1000000L, response.budgetMaxPerTask());
        assertEquals(10000000L, response.budgetMaxPerHour());
    }

    @Test
    void createAgent_invalidModel_throwsValidation() {
        AgentConfigRequest config = new AgentConfigRequest(
                "prompt", "bad-provider", "bad-model", 0.7, List.of(), null, null, null, null);
        AgentCreateRequest request = new AgentCreateRequest("Test Agent", config, null, null, null);

        doThrow(new ValidationException("Unsupported model or provider: bad-provider/bad-model"))
                .when(configValidationHelper).validateAgentConfig(any());

        assertThrows(ValidationException.class,
                () -> agentService.createAgent(request));
    }

    @Test
    void createAgent_invalidTool_throwsValidation() {
        AgentConfigRequest config = new AgentConfigRequest(
                "prompt", "openai", "gpt-4o", 0.7, List.of("unsupported_tool"), null, null, null, null);
        AgentCreateRequest request = new AgentCreateRequest("Test Agent", config, null, null, null);

        doThrow(new ValidationException("Unsupported tool: unsupported_tool"))
                .when(configValidationHelper).validateAgentConfig(any());

        assertThrows(ValidationException.class,
                () -> agentService.createAgent(request));
    }

    // --- createAgent config canonicalization tests ---

    @Test
    void createAgent_nullTemperature_defaultsTo0_7() {
        AgentConfigRequest config = new AgentConfigRequest(
                "prompt", "openai", "gpt-4o", null, List.of("web_search"), null, null, null, null);
        AgentCreateRequest request = new AgentCreateRequest("Test Agent", config, null, null, null);

        doNothing().when(configValidationHelper).validateAgentConfig(any());

        Timestamp now = Timestamp.from(Instant.now());
        Map<String, Object> repoResult = new LinkedHashMap<>();
        repoResult.put("created_at", now);
        repoResult.put("updated_at", now);
        when(agentRepository.insert(eq(TENANT_ID), anyString(), eq("Test Agent"), argThat(json ->
                json.contains("\"temperature\":0.7")), eq(5), eq(500000L), eq(5000000L)))
                .thenReturn(repoResult);

        AgentResponse response = agentService.createAgent(request);

        assertNotNull(response);
        verify(agentRepository).insert(eq(TENANT_ID), anyString(), eq("Test Agent"),
                argThat(json -> json.contains("\"temperature\":0.7")), eq(5), eq(500000L), eq(5000000L));
    }

    @Test
    void createAgent_nullAllowedTools_defaultsToEmptyList() {
        AgentConfigRequest config = new AgentConfigRequest(
                "prompt", "openai", "gpt-4o", 0.7, null, null, null, null, null);
        AgentCreateRequest request = new AgentCreateRequest("Test Agent", config, null, null, null);

        doNothing().when(configValidationHelper).validateAgentConfig(any());

        Timestamp now = Timestamp.from(Instant.now());
        Map<String, Object> repoResult = new LinkedHashMap<>();
        repoResult.put("created_at", now);
        repoResult.put("updated_at", now);
        when(agentRepository.insert(eq(TENANT_ID), anyString(), eq("Test Agent"), argThat(json ->
                json.contains("\"web_search\"") && json.contains("\"request_human_input\"")), eq(5), eq(500000L), eq(5000000L)))
                .thenReturn(repoResult);

        AgentResponse response = agentService.createAgent(request);

        assertNotNull(response);
        verify(agentRepository).insert(eq(TENANT_ID), anyString(), eq("Test Agent"),
                argThat(json -> json.contains("\"web_search\"") && json.contains("\"request_human_input\"")), eq(5), eq(500000L), eq(5000000L));
    }

    // --- getAgent tests ---

    @Test
    void getAgent_success() {
        Map<String, Object> row = buildAgentRow("test-agent", "Test Agent", "active");
        when(agentRepository.findByIdAndTenant(TENANT_ID, "test-agent"))
                .thenReturn(Optional.of(row));

        AgentResponse response = agentService.getAgent("test-agent");

        assertEquals("test-agent", response.agentId());
        assertEquals("Test Agent", response.displayName());
        assertEquals("active", response.status());
        assertEquals(5, response.maxConcurrentTasks());
        assertEquals(500000L, response.budgetMaxPerTask());
        assertEquals(5000000L, response.budgetMaxPerHour());
        assertNotNull(response.createdAt());
    }

    @Test
    void getAgent_notFound_throwsAgentNotFoundException() {
        when(agentRepository.findByIdAndTenant(TENANT_ID, "nonexistent"))
                .thenReturn(Optional.empty());

        assertThrows(AgentNotFoundException.class,
                () -> agentService.getAgent("nonexistent"));
    }

    // --- listAgents tests ---

    @Test
    void listAgents_noFilter_returnsAll() {
        Map<String, Object> row = buildAgentSummaryRow("test-agent", "Test Agent", "openai", "gpt-4o", "active");
        when(agentRepository.listByTenant(TENANT_ID, null, 50)).thenReturn(List.of(row));

        List<AgentSummaryResponse> result = agentService.listAgents(null, null);

        assertEquals(1, result.size());
        assertEquals("test-agent", result.get(0).agentId());
        assertEquals("Test Agent", result.get(0).displayName());
        assertEquals("openai", result.get(0).provider());
        assertEquals("gpt-4o", result.get(0).model());
        assertEquals("active", result.get(0).status());
        assertEquals(5, result.get(0).maxConcurrentTasks());
        assertEquals(500000L, result.get(0).budgetMaxPerTask());
        assertEquals(5000000L, result.get(0).budgetMaxPerHour());
    }

    @Test
    void listAgents_withStatusFilter_passesFilter() {
        when(agentRepository.listByTenant(TENANT_ID, "disabled", 50)).thenReturn(List.of());

        List<AgentSummaryResponse> result = agentService.listAgents("disabled", null);

        assertEquals(0, result.size());
        verify(agentRepository).listByTenant(TENANT_ID, "disabled", 50);
    }

    @Test
    void listAgents_invalidStatus_throwsValidation() {
        assertThrows(ValidationException.class,
                () -> agentService.listAgents("garbage", null));
    }

    @Test
    void listAgents_limitCapped() {
        when(agentRepository.listByTenant(TENANT_ID, null, 200)).thenReturn(List.of());

        agentService.listAgents(null, 500); // should cap at 200

        verify(agentRepository).listByTenant(TENANT_ID, null, 200);
    }

    @Test
    void listAgents_limitFloorAt1() {
        when(agentRepository.listByTenant(TENANT_ID, null, 1)).thenReturn(List.of());

        agentService.listAgents(null, -5); // should floor at 1

        verify(agentRepository).listByTenant(TENANT_ID, null, 1);
    }

    // --- updateAgent tests ---

    @Test
    void updateAgent_success() {
        AgentConfigRequest config = new AgentConfigRequest(
                "Updated prompt.", "openai", "gpt-4o", 0.5, List.of(), null, null, null, null);
        AgentUpdateRequest request = new AgentUpdateRequest("Updated Agent", config, "active", null, null, null);

        doNothing().when(configValidationHelper).validateAgentConfig(any());

        Map<String, Object> existingRow = buildAgentRow("test-agent", "Test Agent", "active");
        when(agentRepository.findByIdAndTenant(TENANT_ID, "test-agent"))
                .thenReturn(Optional.of(existingRow));

        Map<String, Object> updatedRow = buildAgentRow("test-agent", "Updated Agent", "active");
        when(agentRepository.update(eq(TENANT_ID), eq("test-agent"), eq("Updated Agent"), anyString(), eq("active"),
                eq(5), eq(500000L), eq(5000000L)))
                .thenReturn(Optional.of(updatedRow));

        AgentResponse response = agentService.updateAgent("test-agent", request);

        assertEquals("test-agent", response.agentId());
        assertEquals("Updated Agent", response.displayName());
        assertEquals("active", response.status());
    }

    @Test
    void updateAgent_withBudgetFields_success() {
        AgentConfigRequest config = new AgentConfigRequest(
                "Updated prompt.", "openai", "gpt-4o", 0.5, List.of(), null, null, null, null);
        AgentUpdateRequest request = new AgentUpdateRequest("Updated Agent", config, "active", 10, 1000000L, 10000000L);

        doNothing().when(configValidationHelper).validateAgentConfig(any());

        Map<String, Object> existingRow = buildAgentRow("test-agent", "Test Agent", "active");
        when(agentRepository.findByIdAndTenant(TENANT_ID, "test-agent"))
                .thenReturn(Optional.of(existingRow));

        Map<String, Object> updatedRow = buildAgentRow("test-agent", "Updated Agent", "active");
        updatedRow.put("max_concurrent_tasks", 10);
        updatedRow.put("budget_max_per_task", 1000000L);
        updatedRow.put("budget_max_per_hour", 10000000L);
        when(agentRepository.update(eq(TENANT_ID), eq("test-agent"), eq("Updated Agent"), anyString(), eq("active"),
                eq(10), eq(1000000L), eq(10000000L)))
                .thenReturn(Optional.of(updatedRow));

        AgentResponse response = agentService.updateAgent("test-agent", request);

        assertEquals("test-agent", response.agentId());
        assertEquals(10, response.maxConcurrentTasks());
        assertEquals(1000000L, response.budgetMaxPerTask());
        assertEquals(10000000L, response.budgetMaxPerHour());
    }

    @Test
    void updateAgent_notFound_throwsAgentNotFoundException() {
        AgentConfigRequest config = new AgentConfigRequest(
                "prompt", "openai", "gpt-4o", 0.7, List.of(), null, null, null, null);
        AgentUpdateRequest request = new AgentUpdateRequest("Updated Agent", config, "active", null, null, null);

        doNothing().when(configValidationHelper).validateAgentConfig(any());

        when(agentRepository.findByIdAndTenant(TENANT_ID, "nonexistent"))
                .thenReturn(Optional.empty());

        assertThrows(AgentNotFoundException.class,
                () -> agentService.updateAgent("nonexistent", request));
    }

    @Test
    void updateAgent_invalidStatus_throwsValidation() {
        AgentConfigRequest config = new AgentConfigRequest(
                "prompt", "openai", "gpt-4o", 0.7, List.of(), null, null, null, null);
        AgentUpdateRequest request = new AgentUpdateRequest("Agent", config, "invalid_status", null, null, null);

        assertThrows(ValidationException.class,
                () -> agentService.updateAgent("test-agent", request));
    }

    @Test
    void updateAgent_invalidModel_throwsValidation() {
        AgentConfigRequest config = new AgentConfigRequest(
                "prompt", "bad-provider", "bad-model", 0.7, List.of(), null, null, null, null);
        AgentUpdateRequest request = new AgentUpdateRequest("Agent", config, "active", null, null, null);

        doThrow(new ValidationException("Unsupported model or provider"))
                .when(configValidationHelper).validateAgentConfig(any());

        assertThrows(ValidationException.class,
                () -> agentService.updateAgent("test-agent", request));
    }

    @Test
    void updateAgent_configCanonicalization_appliesDefaults() {
        AgentConfigRequest config = new AgentConfigRequest(
                "prompt", "openai", "gpt-4o", null, null, null, null, null, null);
        AgentUpdateRequest request = new AgentUpdateRequest("Agent", config, "active", null, null, null);

        doNothing().when(configValidationHelper).validateAgentConfig(any());

        Map<String, Object> existingRow = buildAgentRow("test-agent", "Agent", "active");
        when(agentRepository.findByIdAndTenant(TENANT_ID, "test-agent"))
                .thenReturn(Optional.of(existingRow));

        Map<String, Object> updatedRow = buildAgentRow("test-agent", "Agent", "active");
        when(agentRepository.update(eq(TENANT_ID), eq("test-agent"), eq("Agent"),
                argThat(json -> json.contains("\"temperature\":0.7") && json.contains("\"web_search\"")),
                eq("active"), eq(5), eq(500000L), eq(5000000L)))
                .thenReturn(Optional.of(updatedRow));

        AgentResponse response = agentService.updateAgent("test-agent", request);

        assertNotNull(response);
        verify(agentRepository).update(eq(TENANT_ID), eq("test-agent"), eq("Agent"),
                argThat(json -> json.contains("\"temperature\":0.7") && json.contains("\"web_search\"")),
                eq("active"), eq(5), eq(500000L), eq(5000000L));
    }

    // --- memory canonicalization / round-trip tests ---

    @Test
    void createAgent_memoryAbsent_notWrittenToConfig() throws Exception {
        // No memory sub-object on the request — persisted JSON must omit the
        // "memory" key entirely. No silent defaults.
        AgentConfigRequest config = new AgentConfigRequest(
                "prompt", "openai", "gpt-4o", 0.7, List.of(), null, null, null, null);
        AgentCreateRequest request = new AgentCreateRequest("Test Agent", config, null, null, null);

        doNothing().when(configValidationHelper).validateAgentConfig(any());

        Timestamp now = Timestamp.from(Instant.now());
        Map<String, Object> repoResult = new LinkedHashMap<>();
        repoResult.put("created_at", now);
        repoResult.put("updated_at", now);

        ArgumentCaptor<String> jsonCaptor = ArgumentCaptor.forClass(String.class);
        when(agentRepository.insert(eq(TENANT_ID), anyString(), eq("Test Agent"), jsonCaptor.capture(),
                eq(5), eq(500000L), eq(5000000L)))
                .thenReturn(repoResult);

        agentService.createAgent(request);

        String persistedJson = jsonCaptor.getValue();
        assertFalse(persistedJson.contains("\"memory\""),
                "memory key must be absent from persisted JSON when request omits it: " + persistedJson);
    }

    @Test
    void createAgent_memoryEnabledOnly_roundTripsIntact() throws Exception {
        // Only enabled=true, other fields absent — persisted JSON has
        // memory.enabled=true but summarizer_model and max_entries absent
        // (or null; defaults are applied at read time).
        MemoryConfigRequest memory = new MemoryConfigRequest(true, null, null);
        AgentConfigRequest config = new AgentConfigRequest(
                "prompt", "openai", "gpt-4o", 0.7, List.of(), null, null, memory, null);
        AgentCreateRequest request = new AgentCreateRequest("Agent", config, null, null, null);

        doNothing().when(configValidationHelper).validateAgentConfig(any());

        Timestamp now = Timestamp.from(Instant.now());
        Map<String, Object> repoResult = new LinkedHashMap<>();
        repoResult.put("created_at", now);
        repoResult.put("updated_at", now);

        ArgumentCaptor<String> jsonCaptor = ArgumentCaptor.forClass(String.class);
        when(agentRepository.insert(eq(TENANT_ID), anyString(), eq("Agent"), jsonCaptor.capture(),
                eq(5), eq(500000L), eq(5000000L)))
                .thenReturn(repoResult);

        agentService.createAgent(request);

        String persistedJson = jsonCaptor.getValue();
        AgentConfigRequest parsed = objectMapper.readValue(persistedJson, AgentConfigRequest.class);

        assertNotNull(parsed.memory(), "memory sub-object must round-trip when present");
        assertEquals(Boolean.TRUE, parsed.memory().enabled());
        assertNull(parsed.memory().summarizerModel());
        assertNull(parsed.memory().maxEntries());
    }

    @Test
    void createAgent_memoryAllFields_roundTripsVerbatim() throws Exception {
        // All three fields set — persisted JSON preserves them exactly.
        MemoryConfigRequest memory = new MemoryConfigRequest(true, "claude-haiku-4-5", 25_000);
        AgentConfigRequest config = new AgentConfigRequest(
                "prompt", "openai", "gpt-4o", 0.7, List.of(), null, null, memory, null);
        AgentCreateRequest request = new AgentCreateRequest("Agent", config, null, null, null);

        doNothing().when(configValidationHelper).validateAgentConfig(any());

        Timestamp now = Timestamp.from(Instant.now());
        Map<String, Object> repoResult = new LinkedHashMap<>();
        repoResult.put("created_at", now);
        repoResult.put("updated_at", now);

        ArgumentCaptor<String> jsonCaptor = ArgumentCaptor.forClass(String.class);
        when(agentRepository.insert(eq(TENANT_ID), anyString(), eq("Agent"), jsonCaptor.capture(),
                eq(5), eq(500000L), eq(5000000L)))
                .thenReturn(repoResult);

        agentService.createAgent(request);

        String persistedJson = jsonCaptor.getValue();
        AgentConfigRequest parsed = objectMapper.readValue(persistedJson, AgentConfigRequest.class);

        assertNotNull(parsed.memory());
        assertEquals(Boolean.TRUE, parsed.memory().enabled());
        assertEquals("claude-haiku-4-5", parsed.memory().summarizerModel());
        assertEquals(Integer.valueOf(25_000), parsed.memory().maxEntries());

        // Must use the snake_case JSON keys.
        assertTrue(persistedJson.contains("\"summarizer_model\":\"claude-haiku-4-5\""),
                "summarizer_model must use snake_case JSON key: " + persistedJson);
        assertTrue(persistedJson.contains("\"max_entries\":25000"),
                "max_entries must use snake_case JSON key: " + persistedJson);
    }

    @Test
    void createAgent_memoryDisabled_roundTripsVerbatim() throws Exception {
        // enabled=false with no other fields — persisted JSON still preserves
        // the sub-object; downstream code distinguishes explicit-false from absent.
        MemoryConfigRequest memory = new MemoryConfigRequest(false, null, null);
        AgentConfigRequest config = new AgentConfigRequest(
                "prompt", "openai", "gpt-4o", 0.7, List.of(), null, null, memory, null);
        AgentCreateRequest request = new AgentCreateRequest("Agent", config, null, null, null);

        doNothing().when(configValidationHelper).validateAgentConfig(any());

        Timestamp now = Timestamp.from(Instant.now());
        Map<String, Object> repoResult = new LinkedHashMap<>();
        repoResult.put("created_at", now);
        repoResult.put("updated_at", now);

        ArgumentCaptor<String> jsonCaptor = ArgumentCaptor.forClass(String.class);
        when(agentRepository.insert(eq(TENANT_ID), anyString(), eq("Agent"), jsonCaptor.capture(),
                eq(5), eq(500000L), eq(5000000L)))
                .thenReturn(repoResult);

        agentService.createAgent(request);

        String persistedJson = jsonCaptor.getValue();
        AgentConfigRequest parsed = objectMapper.readValue(persistedJson, AgentConfigRequest.class);
        assertNotNull(parsed.memory());
        assertEquals(Boolean.FALSE, parsed.memory().enabled());
    }

    @Test
    void updateAgent_memory_roundTripsIntact() throws Exception {
        // PUT path must canonicalize memory identically to POST.
        MemoryConfigRequest memory = new MemoryConfigRequest(true, null, 500);
        AgentConfigRequest config = new AgentConfigRequest(
                "prompt", "openai", "gpt-4o", 0.7, List.of(), null, null, memory, null);
        AgentUpdateRequest request = new AgentUpdateRequest("Agent", config, "active", null, null, null);

        doNothing().when(configValidationHelper).validateAgentConfig(any());

        Map<String, Object> existingRow = buildAgentRow("test-agent", "Agent", "active");
        when(agentRepository.findByIdAndTenant(TENANT_ID, "test-agent"))
                .thenReturn(Optional.of(existingRow));

        Map<String, Object> updatedRow = buildAgentRow("test-agent", "Agent", "active");
        ArgumentCaptor<String> jsonCaptor = ArgumentCaptor.forClass(String.class);
        when(agentRepository.update(eq(TENANT_ID), eq("test-agent"), eq("Agent"),
                jsonCaptor.capture(), eq("active"), eq(5), eq(500000L), eq(5000000L)))
                .thenReturn(Optional.of(updatedRow));

        agentService.updateAgent("test-agent", request);

        String persistedJson = jsonCaptor.getValue();
        AgentConfigRequest parsed = objectMapper.readValue(persistedJson, AgentConfigRequest.class);
        assertNotNull(parsed.memory());
        assertEquals(Boolean.TRUE, parsed.memory().enabled());
        assertEquals(Integer.valueOf(500), parsed.memory().maxEntries());
        assertNull(parsed.memory().summarizerModel());
    }

    @Test
    void memoryConfig_jacksonDeserialization_acceptsSnakeCaseKeys() throws Exception {
        // Ensure that the record can be deserialized from the snake_case keys
        // stored in the persisted JSON — round-trips both ways.
        String json = "{\"enabled\":true,\"summarizer_model\":\"gpt-4o-mini\",\"max_entries\":5000}";
        MemoryConfigRequest parsed = objectMapper.readValue(json, MemoryConfigRequest.class);
        assertEquals(Boolean.TRUE, parsed.enabled());
        assertEquals("gpt-4o-mini", parsed.summarizerModel());
        assertEquals(Integer.valueOf(5000), parsed.maxEntries());
    }

    @Test
    void agentConfig_jacksonDeserialization_preservesMemorySubObject() throws Exception {
        // Full agent_config JSON with nested memory — the AgentConfigRequest
        // record must deserialize without dropping the memory field (the
        // Jackson FAIL_ON_UNKNOWN_PROPERTIES trap this task is designed to avoid).
        String json = "{\"system_prompt\":\"p\",\"provider\":\"openai\",\"model\":\"gpt-4o\","
                + "\"temperature\":0.7,\"allowed_tools\":[],"
                + "\"memory\":{\"enabled\":true,\"summarizer_model\":\"claude-haiku-4-5\",\"max_entries\":2000}}";
        AgentConfigRequest parsed = objectMapper.readValue(json, AgentConfigRequest.class);
        assertNotNull(parsed.memory(), "memory sub-object must survive Jackson round-trip");
        assertEquals(Boolean.TRUE, parsed.memory().enabled());
        assertEquals("claude-haiku-4-5", parsed.memory().summarizerModel());
        assertEquals(Integer.valueOf(2000), parsed.memory().maxEntries());
    }

    // --- context_management canonicalization / round-trip tests ---

    @Test
    void createAgent_contextManagementAbsent_notWrittenToConfig() throws Exception {
        // No context_management sub-object on the request — persisted JSON must omit
        // the "context_management" key entirely. No silent defaults.
        AgentConfigRequest config = new AgentConfigRequest(
                "prompt", "openai", "gpt-4o", 0.7, List.of(), null, null, null, null);
        AgentCreateRequest request = new AgentCreateRequest("Agent", config, null, null, null);

        doNothing().when(configValidationHelper).validateAgentConfig(any());

        Timestamp now = Timestamp.from(Instant.now());
        Map<String, Object> repoResult = new LinkedHashMap<>();
        repoResult.put("created_at", now);
        repoResult.put("updated_at", now);

        ArgumentCaptor<String> jsonCaptor = ArgumentCaptor.forClass(String.class);
        when(agentRepository.insert(eq(TENANT_ID), anyString(), eq("Agent"), jsonCaptor.capture(),
                eq(5), eq(500000L), eq(5000000L)))
                .thenReturn(repoResult);

        agentService.createAgent(request);

        String persistedJson = jsonCaptor.getValue();
        assertFalse(persistedJson.contains("\"context_management\""),
                "context_management key must be absent from persisted JSON when request omits it: " + persistedJson);
    }

    @Test
    void createAgent_contextManagementEmptyObject_roundTripsIntact() throws Exception {
        // Empty context_management sub-object (all fields null) — persisted JSON has the
        // context_management key with null fields, but the sub-object itself is present.
        ContextManagementConfigRequest cm = new ContextManagementConfigRequest(null, null, null, null, null);
        AgentConfigRequest config = new AgentConfigRequest(
                "prompt", "openai", "gpt-4o", 0.7, List.of(), null, null, null, cm);
        AgentCreateRequest request = new AgentCreateRequest("Agent", config, null, null, null);

        doNothing().when(configValidationHelper).validateAgentConfig(any());

        Timestamp now = Timestamp.from(Instant.now());
        Map<String, Object> repoResult = new LinkedHashMap<>();
        repoResult.put("created_at", now);
        repoResult.put("updated_at", now);

        ArgumentCaptor<String> jsonCaptor = ArgumentCaptor.forClass(String.class);
        when(agentRepository.insert(eq(TENANT_ID), anyString(), eq("Agent"), jsonCaptor.capture(),
                eq(5), eq(500000L), eq(5000000L)))
                .thenReturn(repoResult);

        agentService.createAgent(request);

        String persistedJson = jsonCaptor.getValue();
        assertTrue(persistedJson.contains("\"context_management\""),
                "context_management key must be present when request includes the sub-object: " + persistedJson);
        AgentConfigRequest parsed = objectMapper.readValue(persistedJson, AgentConfigRequest.class);
        assertNotNull(parsed.contextManagement(), "context_management sub-object must survive Jackson round-trip");
        assertNull(parsed.contextManagement().summarizerModel());
        assertNull(parsed.contextManagement().excludeTools());
        assertNull(parsed.contextManagement().preTier3MemoryFlush());
        assertNull(parsed.contextManagement().offloadToolResults());
    }

    @Test
    void createAgent_contextManagementAllFields_roundTripsVerbatim() throws Exception {
        // All three fields set — persisted JSON preserves them exactly with snake_case keys.
        List<String> excludeTools = List.of("web_search", "custom_tool_x");
        ContextManagementConfigRequest cm = new ContextManagementConfigRequest(
                "claude-haiku-4-5", null, excludeTools, true, false);
        AgentConfigRequest config = new AgentConfigRequest(
                "prompt", "openai", "gpt-4o", 0.7, List.of(), null, null, null, cm);
        AgentCreateRequest request = new AgentCreateRequest("Agent", config, null, null, null);

        doNothing().when(configValidationHelper).validateAgentConfig(any());

        Timestamp now = Timestamp.from(Instant.now());
        Map<String, Object> repoResult = new LinkedHashMap<>();
        repoResult.put("created_at", now);
        repoResult.put("updated_at", now);

        ArgumentCaptor<String> jsonCaptor = ArgumentCaptor.forClass(String.class);
        when(agentRepository.insert(eq(TENANT_ID), anyString(), eq("Agent"), jsonCaptor.capture(),
                eq(5), eq(500000L), eq(5000000L)))
                .thenReturn(repoResult);

        agentService.createAgent(request);

        String persistedJson = jsonCaptor.getValue();
        AgentConfigRequest parsed = objectMapper.readValue(persistedJson, AgentConfigRequest.class);

        assertNotNull(parsed.contextManagement());
        assertEquals("claude-haiku-4-5", parsed.contextManagement().summarizerModel());
        assertEquals(excludeTools, parsed.contextManagement().excludeTools());
        assertEquals(Boolean.TRUE, parsed.contextManagement().preTier3MemoryFlush());
        assertEquals(Boolean.FALSE, parsed.contextManagement().offloadToolResults());

        // Must use snake_case JSON keys.
        assertTrue(persistedJson.contains("\"summarizer_model\":\"claude-haiku-4-5\""),
                "summarizer_model must use snake_case JSON key: " + persistedJson);
        assertTrue(persistedJson.contains("\"exclude_tools\""),
                "exclude_tools must use snake_case JSON key: " + persistedJson);
        assertTrue(persistedJson.contains("\"pre_tier3_memory_flush\":true"),
                "pre_tier3_memory_flush must use snake_case JSON key: " + persistedJson);
        assertTrue(persistedJson.contains("\"offload_tool_results\":false"),
                "offload_tool_results must use snake_case JSON key: " + persistedJson);
    }

    @Test
    void updateAgent_contextManagement_roundTripsIntact() throws Exception {
        // PUT path must canonicalize context_management identically to POST.
        ContextManagementConfigRequest cm = new ContextManagementConfigRequest(null, null, List.of("custom_tool"), false, null);
        AgentConfigRequest config = new AgentConfigRequest(
                "prompt", "openai", "gpt-4o", 0.7, List.of(), null, null, null, cm);
        AgentUpdateRequest request = new AgentUpdateRequest("Agent", config, "active", null, null, null);

        doNothing().when(configValidationHelper).validateAgentConfig(any());

        Map<String, Object> existingRow = buildAgentRow("test-agent", "Agent", "active");
        when(agentRepository.findByIdAndTenant(TENANT_ID, "test-agent"))
                .thenReturn(Optional.of(existingRow));

        Map<String, Object> updatedRow = buildAgentRow("test-agent", "Agent", "active");
        ArgumentCaptor<String> jsonCaptor = ArgumentCaptor.forClass(String.class);
        when(agentRepository.update(eq(TENANT_ID), eq("test-agent"), eq("Agent"),
                jsonCaptor.capture(), eq("active"), eq(5), eq(500000L), eq(5000000L)))
                .thenReturn(Optional.of(updatedRow));

        agentService.updateAgent("test-agent", request);

        String persistedJson = jsonCaptor.getValue();
        AgentConfigRequest parsed = objectMapper.readValue(persistedJson, AgentConfigRequest.class);
        assertNotNull(parsed.contextManagement());
        assertEquals(List.of("custom_tool"), parsed.contextManagement().excludeTools());
        assertEquals(Boolean.FALSE, parsed.contextManagement().preTier3MemoryFlush());
        assertNull(parsed.contextManagement().summarizerModel());
    }

    @Test
    void contextManagementConfig_jacksonDeserialization_acceptsSnakeCaseKeys() throws Exception {
        // Ensure ContextManagementConfigRequest can be deserialized from snake_case JSON.
        String json = "{\"summarizer_model\":\"claude-haiku-4-5\","
                + "\"exclude_tools\":[\"web_search\"],\"pre_tier3_memory_flush\":true}";
        ContextManagementConfigRequest parsed = objectMapper.readValue(json, ContextManagementConfigRequest.class);
        assertEquals("claude-haiku-4-5", parsed.summarizerModel());
        assertEquals(List.of("web_search"), parsed.excludeTools());
        assertEquals(Boolean.TRUE, parsed.preTier3MemoryFlush());
    }

    @Test
    void agentConfig_jacksonDeserialization_preservesContextManagementSubObject() throws Exception {
        // Full agent_config JSON with nested context_management — Jackson FAIL_ON_UNKNOWN_PROPERTIES
        // must not reject the sub-object; the typed field must survive round-trip.
        String json = "{\"system_prompt\":\"p\",\"provider\":\"openai\",\"model\":\"gpt-4o\","
                + "\"temperature\":0.7,\"allowed_tools\":[],"
                + "\"context_management\":{\"summarizer_model\":\"claude-haiku-4-5\","
                + "\"exclude_tools\":[\"memory_note\"],\"pre_tier3_memory_flush\":false}}";
        AgentConfigRequest parsed = objectMapper.readValue(json, AgentConfigRequest.class);
        assertNotNull(parsed.contextManagement(), "context_management sub-object must survive Jackson round-trip");
        assertEquals("claude-haiku-4-5", parsed.contextManagement().summarizerModel());
        assertEquals(List.of("memory_note"), parsed.contextManagement().excludeTools());
        assertEquals(Boolean.FALSE, parsed.contextManagement().preTier3MemoryFlush());
    }

    @Test
    void agentConfig_jacksonDeserialization_rejectsEnabledField() throws Exception {
        // Track 7 has no 'enabled' toggle. FAIL_ON_UNKNOWN_PROPERTIES must reject
        // an 'enabled' key inside context_management with an appropriate Jackson error.
        String json = "{\"system_prompt\":\"p\",\"provider\":\"openai\",\"model\":\"gpt-4o\","
                + "\"temperature\":0.7,\"allowed_tools\":[],"
                + "\"context_management\":{\"enabled\":true}}";
        assertThrows(com.fasterxml.jackson.databind.exc.UnrecognizedPropertyException.class,
                () -> objectMapper.readValue(json, AgentConfigRequest.class),
                "Jackson must reject unknown 'enabled' field inside context_management");
    }

    // --- plan_write opt-in tool canonicalization tests ---

    /**
     * TDD RED: createAgent with plan_write in allowed_tools must store it alongside the base
     * platform tools. Validates the OPT_IN_TOOLS preservation loop in canonicalizeConfig.
     */
    @Test
    void createAgent_planWriteRequested_survivesCanonicalization() throws Exception {
        AgentConfigRequest config = new AgentConfigRequest(
                "You are a planning agent.", "anthropic", "claude-sonnet-4-6", 0.0,
                List.of("plan_write"), null, null, null, null);
        AgentCreateRequest request = new AgentCreateRequest("Planning Agent", config, null, null, null);

        doNothing().when(configValidationHelper).validateAgentConfig(any());

        Timestamp now = Timestamp.from(Instant.now());
        Map<String, Object> repoResult = new LinkedHashMap<>();
        repoResult.put("created_at", now);
        repoResult.put("updated_at", now);

        ArgumentCaptor<String> jsonCaptor = ArgumentCaptor.forClass(String.class);
        when(agentRepository.insert(eq(TENANT_ID), anyString(), eq("Planning Agent"),
                jsonCaptor.capture(), eq(5), eq(500000L), eq(5000000L)))
                .thenReturn(repoResult);

        agentService.createAgent(request);

        String persistedJson = jsonCaptor.getValue();
        AgentConfigRequest parsed = objectMapper.readValue(persistedJson, AgentConfigRequest.class);
        // plan_write must survive — the opt-in tool is explicitly requested
        assertTrue(parsed.allowedTools().contains("plan_write"),
                "plan_write must survive canonicalization when caller requests it; got: "
                        + parsed.allowedTools());
        // Base platform tools must also be present
        assertTrue(parsed.allowedTools().containsAll(
                com.persistentagent.api.config.ValidationConstants.BASE_PLATFORM_TOOLS),
                "Base platform tools must always be present; got: " + parsed.allowedTools());
    }

    /**
     * TDD RED: createAgent WITHOUT plan_write must NOT silently seed it — opt-in means
     * the caller must ask for it explicitly.
     */
    @Test
    void createAgent_planWriteNotRequested_notSeededInConfig() throws Exception {
        AgentConfigRequest config = new AgentConfigRequest(
                "A plain agent.", "anthropic", "claude-sonnet-4-6", 0.0,
                List.of("web_search"), null, null, null, null);
        AgentCreateRequest request = new AgentCreateRequest("Plain Agent", config, null, null, null);

        doNothing().when(configValidationHelper).validateAgentConfig(any());

        Timestamp now = Timestamp.from(Instant.now());
        Map<String, Object> repoResult = new LinkedHashMap<>();
        repoResult.put("created_at", now);
        repoResult.put("updated_at", now);

        ArgumentCaptor<String> jsonCaptor = ArgumentCaptor.forClass(String.class);
        when(agentRepository.insert(eq(TENANT_ID), anyString(), eq("Plain Agent"),
                jsonCaptor.capture(), eq(5), eq(500000L), eq(5000000L)))
                .thenReturn(repoResult);

        agentService.createAgent(request);

        String persistedJson = jsonCaptor.getValue();
        AgentConfigRequest parsed = objectMapper.readValue(persistedJson, AgentConfigRequest.class);
        assertFalse(parsed.allowedTools().contains("plan_write"),
                "plan_write must NOT be seeded when caller did not request it; got: "
                        + parsed.allowedTools());
    }

    /**
     * TDD RED: OPT_IN_TOOLS must be a subset of ALLOWED_TOOLS and disjoint from
     * BASE_PLATFORM_TOOLS — so opt-in tools pass validation but are NOT auto-added.
     * Also validates that DEV_TASK_CONTROL_TOOLS and BASE_PLATFORM_TOOLS remain
     * disjoint (existing auto-determine design is intact).
     */
    @Test
    void validationConstants_optInToolsSubsetRelationship() {
        // OPT_IN_TOOLS ⊆ ALLOWED_TOOLS
        assertTrue(
                com.persistentagent.api.config.ValidationConstants.ALLOWED_TOOLS.containsAll(
                        com.persistentagent.api.config.ValidationConstants.OPT_IN_TOOLS),
                "Every OPT_IN_TOOLS entry must appear in ALLOWED_TOOLS for validation to pass");

        // OPT_IN_TOOLS ∩ BASE_PLATFORM_TOOLS = ∅ (opt-in means not auto-added)
        java.util.Set<String> overlap = new java.util.HashSet<>(
                com.persistentagent.api.config.ValidationConstants.OPT_IN_TOOLS);
        overlap.retainAll(com.persistentagent.api.config.ValidationConstants.BASE_PLATFORM_TOOLS);
        assertTrue(overlap.isEmpty(),
                "OPT_IN_TOOLS must be disjoint from BASE_PLATFORM_TOOLS; overlap: " + overlap);

        // plan_write is in OPT_IN_TOOLS
        assertTrue(
                com.persistentagent.api.config.ValidationConstants.OPT_IN_TOOLS.contains("plan_write"),
                "plan_write must be in OPT_IN_TOOLS");
    }

    /**
     * TDD RED: updateAgent with plan_write in allowed_tools must keep it after PUT
     * (full-config replacement semantics — the caller sends the complete config each time).
     */
    @Test
    void updateAgent_planWriteRequested_survivesCanonicalization() throws Exception {
        AgentConfigRequest config = new AgentConfigRequest(
                "Updated planning agent.", "anthropic", "claude-sonnet-4-6", 0.0,
                List.of("plan_write"), null, null, null, null);
        AgentUpdateRequest request = new AgentUpdateRequest("Planning Agent", config, "active", null, null, null);

        doNothing().when(configValidationHelper).validateAgentConfig(any());

        Map<String, Object> existingRow = buildAgentRow("planning-agent", "Planning Agent", "active");
        when(agentRepository.findByIdAndTenant(TENANT_ID, "planning-agent"))
                .thenReturn(Optional.of(existingRow));

        Map<String, Object> updatedRow = buildAgentRow("planning-agent", "Planning Agent", "active");
        ArgumentCaptor<String> jsonCaptor = ArgumentCaptor.forClass(String.class);
        when(agentRepository.update(eq(TENANT_ID), eq("planning-agent"), eq("Planning Agent"),
                jsonCaptor.capture(), eq("active"), eq(5), eq(500000L), eq(5000000L)))
                .thenReturn(Optional.of(updatedRow));

        agentService.updateAgent("planning-agent", request);

        String persistedJson = jsonCaptor.getValue();
        AgentConfigRequest parsed = objectMapper.readValue(persistedJson, AgentConfigRequest.class);
        // PUT full-config semantics: the caller explicitly includes plan_write, so it must survive
        assertTrue(parsed.allowedTools().contains("plan_write"),
                "plan_write must survive PUT canonicalization when caller explicitly includes it; got: "
                        + parsed.allowedTools());
    }

    // --- opt-in grant preservation on updates omitting allowed_tools (PR-review finding) ---

    /**
     * Regression test for the PR-review finding: the Console agent editor
     * (AgentDetailPage.tsx) sends an update payload whose agent_config has NO allowed_tools
     * key at all (system_prompt/provider/model/temperature/tool_servers only). With
     * allowedTools() == null, the opt-in preservation loop never ran and canonicalization
     * silently stripped plan_write from a planning-enabled agent.
     *
     * <p>Decided semantics: allowed_tools is not client-owned round-trip state — absence
     * means "no statement about grants", not revocation. The update must carry forward the
     * stored config's existing opt-in grants.
     */
    @Test
    void updateAgent_consoleShapedPayloadWithoutAllowedTools_preservesExistingPlanWriteGrant()
            throws Exception {
        // Console-shaped payload: no allowed_tools key (null), tool_servers present.
        AgentConfigRequest config = new AgentConfigRequest(
                "Edited prompt from Console.", "anthropic", "claude-sonnet-4-6", 0.3,
                null, List.of(), null, null, null);
        AgentUpdateRequest request = new AgentUpdateRequest("Planning Agent", config, "active", null, null, null);

        doNothing().when(configValidationHelper).validateAgentConfig(any());

        // Stored agent already has the plan_write opt-in grant.
        Map<String, Object> existingRow = buildAgentRowWithConfig("planning-agent", "Planning Agent", "active",
                "{\"system_prompt\":\"prompt\",\"provider\":\"anthropic\",\"model\":\"claude-sonnet-4-6\","
                        + "\"temperature\":0.0,\"allowed_tools\":[\"web_search\",\"read_url\","
                        + "\"create_text_artifact\",\"request_human_input\",\"plan_write\"]}");
        when(agentRepository.findByIdAndTenant(TENANT_ID, "planning-agent"))
                .thenReturn(Optional.of(existingRow));

        Map<String, Object> updatedRow = buildAgentRow("planning-agent", "Planning Agent", "active");
        ArgumentCaptor<String> jsonCaptor = ArgumentCaptor.forClass(String.class);
        when(agentRepository.update(eq(TENANT_ID), eq("planning-agent"), eq("Planning Agent"),
                jsonCaptor.capture(), eq("active"), eq(5), eq(500000L), eq(5000000L)))
                .thenReturn(Optional.of(updatedRow));

        agentService.updateAgent("planning-agent", request);

        String persistedJson = jsonCaptor.getValue();
        AgentConfigRequest parsed = objectMapper.readValue(persistedJson, AgentConfigRequest.class);
        assertTrue(parsed.allowedTools().contains("plan_write"),
                "Console-shaped update (allowed_tools absent) must preserve the agent's existing "
                        + "plan_write opt-in grant; got: " + parsed.allowedTools());
        assertTrue(parsed.allowedTools().containsAll(
                com.persistentagent.api.config.ValidationConstants.BASE_PLATFORM_TOOLS),
                "Base platform tools must still be auto-determined; got: " + parsed.allowedTools());
    }

    /**
     * Revocation path stays intact: an update whose allowed_tools is PRESENT is
     * authoritative — an explicit list without plan_write removes the grant from a
     * previously-granted agent.
     */
    @Test
    void updateAgent_explicitAllowedToolsWithoutPlanWrite_revokesGrant() throws Exception {
        AgentConfigRequest config = new AgentConfigRequest(
                "No more planning.", "anthropic", "claude-sonnet-4-6", 0.0,
                List.of("web_search"), null, null, null, null);
        AgentUpdateRequest request = new AgentUpdateRequest("Planning Agent", config, "active", null, null, null);

        doNothing().when(configValidationHelper).validateAgentConfig(any());

        // Stored agent currently has the plan_write grant.
        Map<String, Object> existingRow = buildAgentRowWithConfig("planning-agent", "Planning Agent", "active",
                "{\"system_prompt\":\"prompt\",\"provider\":\"anthropic\",\"model\":\"claude-sonnet-4-6\","
                        + "\"temperature\":0.0,\"allowed_tools\":[\"web_search\",\"plan_write\"]}");
        when(agentRepository.findByIdAndTenant(TENANT_ID, "planning-agent"))
                .thenReturn(Optional.of(existingRow));

        Map<String, Object> updatedRow = buildAgentRow("planning-agent", "Planning Agent", "active");
        ArgumentCaptor<String> jsonCaptor = ArgumentCaptor.forClass(String.class);
        when(agentRepository.update(eq(TENANT_ID), eq("planning-agent"), eq("Planning Agent"),
                jsonCaptor.capture(), eq("active"), eq(5), eq(500000L), eq(5000000L)))
                .thenReturn(Optional.of(updatedRow));

        agentService.updateAgent("planning-agent", request);

        String persistedJson = jsonCaptor.getValue();
        AgentConfigRequest parsed = objectMapper.readValue(persistedJson, AgentConfigRequest.class);
        assertFalse(parsed.allowedTools().contains("plan_write"),
                "Explicit allowed_tools without plan_write must revoke the grant; got: "
                        + parsed.allowedTools());
    }

    /**
     * Create-path semantics pinned unchanged: null allowed_tools on CREATE means no opt-in
     * grants (there is no pre-existing config to carry grants from).
     */
    @Test
    void createAgent_nullAllowedTools_noOptInToolsGranted() throws Exception {
        AgentConfigRequest config = new AgentConfigRequest(
                "prompt", "openai", "gpt-4o", 0.7, null, null, null, null, null);
        AgentCreateRequest request = new AgentCreateRequest("Test Agent", config, null, null, null);

        doNothing().when(configValidationHelper).validateAgentConfig(any());

        Timestamp now = Timestamp.from(Instant.now());
        Map<String, Object> repoResult = new LinkedHashMap<>();
        repoResult.put("created_at", now);
        repoResult.put("updated_at", now);

        ArgumentCaptor<String> jsonCaptor = ArgumentCaptor.forClass(String.class);
        when(agentRepository.insert(eq(TENANT_ID), anyString(), eq("Test Agent"), jsonCaptor.capture(),
                eq(5), eq(500000L), eq(5000000L)))
                .thenReturn(repoResult);

        agentService.createAgent(request);

        String persistedJson = jsonCaptor.getValue();
        AgentConfigRequest parsed = objectMapper.readValue(persistedJson, AgentConfigRequest.class);
        for (String optIn : com.persistentagent.api.config.ValidationConstants.OPT_IN_TOOLS) {
            assertFalse(parsed.allowedTools().contains(optIn),
                    "Opt-in tool " + optIn + " must not be granted on create with null allowed_tools; got: "
                            + parsed.allowedTools());
        }
    }

    // --- helpers ---

    private Map<String, Object> buildAgentRowWithConfig(String agentId, String displayName,
            String status, String agentConfigJson) {
        Map<String, Object> row = buildAgentRow(agentId, displayName, status);
        row.put("agent_config", agentConfigJson);
        return row;
    }

    private Map<String, Object> buildAgentRow(String agentId, String displayName, String status) {
        Map<String, Object> row = new LinkedHashMap<>();
        row.put("agent_id", agentId);
        row.put("display_name", displayName);
        row.put("agent_config", "{\"system_prompt\":\"prompt\",\"provider\":\"openai\",\"model\":\"gpt-4o\",\"temperature\":0.7,\"allowed_tools\":[]}");
        row.put("status", status);
        row.put("max_concurrent_tasks", 5);
        row.put("budget_max_per_task", 500000L);
        row.put("budget_max_per_hour", 5000000L);
        row.put("created_at", Timestamp.from(Instant.now()));
        row.put("updated_at", Timestamp.from(Instant.now()));
        return row;
    }

    private Map<String, Object> buildAgentSummaryRow(String agentId, String displayName,
            String provider, String model, String status) {
        Map<String, Object> row = new LinkedHashMap<>();
        row.put("agent_id", agentId);
        row.put("display_name", displayName);
        row.put("provider", provider);
        row.put("model", model);
        row.put("status", status);
        row.put("max_concurrent_tasks", 5);
        row.put("budget_max_per_task", 500000L);
        row.put("budget_max_per_hour", 5000000L);
        row.put("created_at", Timestamp.from(Instant.now()));
        row.put("updated_at", Timestamp.from(Instant.now()));
        return row;
    }
}
