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
import com.persistentagent.api.model.request.SupervisorConfigRequest;
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
                "You are a helpful assistant.", "openai", "gpt-4o", 0.7, List.of("web_search"), null, null, null, null, null, null, null, null);
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
                "You are a helpful assistant.", "openai", "gpt-4o", 0.7, List.of("web_search"), null, null, null, null, null, null, null, null);
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
                "prompt", "bad-provider", "bad-model", 0.7, List.of(), null, null, null, null, null, null, null, null);
        AgentCreateRequest request = new AgentCreateRequest("Test Agent", config, null, null, null);

        doThrow(new ValidationException("Unsupported model or provider: bad-provider/bad-model"))
                .when(configValidationHelper).validateAgentConfig(any());

        assertThrows(ValidationException.class,
                () -> agentService.createAgent(request));
    }

    @Test
    void createAgent_invalidTool_throwsValidation() {
        AgentConfigRequest config = new AgentConfigRequest(
                "prompt", "openai", "gpt-4o", 0.7, List.of("unsupported_tool"), null, null, null, null, null, null, null, null);
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
                "prompt", "openai", "gpt-4o", null, List.of("web_search"), null, null, null, null, null, null, null, null);
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
                "prompt", "openai", "gpt-4o", 0.7, null, null, null, null, null, null, null, null, null);
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
                "Updated prompt.", "openai", "gpt-4o", 0.5, List.of(), null, null, null, null, null, null, null, null);
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
                "Updated prompt.", "openai", "gpt-4o", 0.5, List.of(), null, null, null, null, null, null, null, null);
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
                "prompt", "openai", "gpt-4o", 0.7, List.of(), null, null, null, null, null, null, null, null);
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
                "prompt", "openai", "gpt-4o", 0.7, List.of(), null, null, null, null, null, null, null, null);
        AgentUpdateRequest request = new AgentUpdateRequest("Agent", config, "invalid_status", null, null, null);

        assertThrows(ValidationException.class,
                () -> agentService.updateAgent("test-agent", request));
    }

    @Test
    void updateAgent_invalidModel_throwsValidation() {
        AgentConfigRequest config = new AgentConfigRequest(
                "prompt", "bad-provider", "bad-model", 0.7, List.of(), null, null, null, null, null, null, null, null);
        AgentUpdateRequest request = new AgentUpdateRequest("Agent", config, "active", null, null, null);

        doThrow(new ValidationException("Unsupported model or provider"))
                .when(configValidationHelper).validateAgentConfig(any());

        assertThrows(ValidationException.class,
                () -> agentService.updateAgent("test-agent", request));
    }

    @Test
    void updateAgent_configCanonicalization_appliesDefaults() {
        AgentConfigRequest config = new AgentConfigRequest(
                "prompt", "openai", "gpt-4o", null, null, null, null, null, null, null, null, null, null);
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
                "prompt", "openai", "gpt-4o", 0.7, List.of(), null, null, null, null, null, null, null, null);
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
                "prompt", "openai", "gpt-4o", 0.7, List.of(), null, null, memory, null, null, null, null, null);
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
                "prompt", "openai", "gpt-4o", 0.7, List.of(), null, null, memory, null, null, null, null, null);
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
                "prompt", "openai", "gpt-4o", 0.7, List.of(), null, null, memory, null, null, null, null, null);
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
                "prompt", "openai", "gpt-4o", 0.7, List.of(), null, null, memory, null, null, null, null, null);
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
                "prompt", "openai", "gpt-4o", 0.7, List.of(), null, null, null, null, null, null, null, null);
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
                "prompt", "openai", "gpt-4o", 0.7, List.of(), null, null, null, cm, null, null, null, null);
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
                "prompt", "openai", "gpt-4o", 0.7, List.of(), null, null, null, cm, null, null, null, null);
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
                "prompt", "openai", "gpt-4o", 0.7, List.of(), null, null, null, cm, null, null, null, null);
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

    // --- plan_write base-tool canonicalization tests ---
    //
    // Product decision (2026-06-06, supersedes the track's §A6 opt-in design):
    // plan_write is a default agent capability for ALL agents, like web_search.
    // It lives in BASE_PLATFORM_TOOLS, so every canonicalized config contains it
    // and — like all base tools — it is non-removable via allowed_tools.

    /**
     * Create with NO allowed_tools (null) → plan_write is present: it is a base
     * platform tool seeded into every agent.
     */
    @Test
    void createAgent_nullAllowedTools_planWriteSeededAsBaseTool() throws Exception {
        AgentConfigRequest config = new AgentConfigRequest(
                "prompt", "openai", "gpt-4o", 0.7, null, null, null, null, null, null, null, null, null);
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
        assertTrue(parsed.allowedTools().contains("plan_write"),
                "plan_write is a base platform tool and must be seeded on every agent; got: "
                        + parsed.allowedTools());
        assertTrue(parsed.allowedTools().containsAll(
                com.persistentagent.api.config.ValidationConstants.BASE_PLATFORM_TOOLS),
                "All base platform tools must be present; got: " + parsed.allowedTools());
    }

    /**
     * Create with an explicit allowed_tools list that OMITS plan_write → it is STILL
     * present: base tools are non-removable (same as web_search — the caller's list
     * cannot strip platform capabilities).
     */
    @Test
    void createAgent_explicitAllowedToolsOmittingPlanWrite_stillPresent() throws Exception {
        AgentConfigRequest config = new AgentConfigRequest(
                "A plain agent.", "anthropic", "claude-sonnet-4-6", 0.0,
                List.of("web_search"), null, null, null, null, null, null, null, null);
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
        assertTrue(parsed.allowedTools().contains("plan_write"),
                "plan_write is a non-removable base tool — present even when the caller's "
                        + "explicit allowed_tools omits it; got: " + parsed.allowedTools());
    }

    /**
     * Create with plan_write explicitly listed → present exactly once (the base-tool
     * seeding and the caller's request must not produce a duplicate).
     */
    @Test
    void createAgent_planWriteExplicitlyRequested_presentWithoutDuplicates() throws Exception {
        AgentConfigRequest config = new AgentConfigRequest(
                "You are a planning agent.", "anthropic", "claude-sonnet-4-6", 0.0,
                List.of("plan_write"), null, null, null, null, null, null, null, null);
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
        long occurrences = parsed.allowedTools().stream()
                .filter("plan_write"::equals)
                .count();
        assertEquals(1, occurrences,
                "plan_write must appear exactly once (base-seeded, never duplicated); got: "
                        + parsed.allowedTools());
    }

    /**
     * Console-shaped update payload (agent_config with system_prompt/provider/model/
     * temperature/tool_servers, NO allowed_tools key — see AgentDetailPage.tsx) →
     * plan_write present in the stored config. As a base tool it is re-seeded at every
     * canonicalization regardless of what the client sends.
     */
    @Test
    void updateAgent_consoleShapedPayloadWithoutAllowedTools_planWritePresent() throws Exception {
        AgentConfigRequest config = new AgentConfigRequest(
                "Edited prompt from Console.", "anthropic", "claude-sonnet-4-6", 0.3,
                null, List.of(), null, null, null, null, null, null, null);
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
        assertTrue(parsed.allowedTools().contains("plan_write"),
                "Console-shaped update (allowed_tools absent) must still produce a config "
                        + "containing the base tool plan_write; got: " + parsed.allowedTools());
        assertTrue(parsed.allowedTools().containsAll(
                com.persistentagent.api.config.ValidationConstants.BASE_PLATFORM_TOOLS),
                "Base platform tools must still be auto-determined; got: " + parsed.allowedTools());
    }

    /**
     * Update with an explicit allowed_tools list that OMITS plan_write → STILL present.
     * Base tools cannot be removed via allowed_tools (same as web_search).
     */
    @Test
    void updateAgent_explicitAllowedToolsOmittingPlanWrite_stillPresent() throws Exception {
        AgentConfigRequest config = new AgentConfigRequest(
                "Try to drop planning.", "anthropic", "claude-sonnet-4-6", 0.0,
                List.of("web_search"), null, null, null, null, null, null, null, null);
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
        assertTrue(parsed.allowedTools().contains("plan_write"),
                "plan_write is a non-removable base tool — an explicit allowed_tools list "
                        + "omitting it must not strip it; got: " + parsed.allowedTools());
    }

    /**
     * Constants pin: every base platform tool (including plan_write) must be in the
     * validation universe, or canonicalized configs would fail their own validation.
     */
    @Test
    void validationConstants_basePlatformToolsSubsetOfAllowedTools() {
        assertTrue(
                com.persistentagent.api.config.ValidationConstants.ALLOWED_TOOLS.containsAll(
                        com.persistentagent.api.config.ValidationConstants.BASE_PLATFORM_TOOLS),
                "BASE_PLATFORM_TOOLS must be a subset of ALLOWED_TOOLS");
        assertTrue(
                com.persistentagent.api.config.ValidationConstants.BASE_PLATFORM_TOOLS.contains("plan_write"),
                "plan_write is a base platform tool by product decision (2026-06-06)");
    }

    // --- topology immutability tests (Agent Modes — S1) ---

    /**
     * PUT changing topology react → supervisor must be rejected with 400 and the exact
     * message "topology is immutable after agent creation" (plan §A0 #2, §A5).
     */
    @Test
    void updateAgent_topologyChange_reactToSupervisor_throws() {
        // Existing row has no topology key (treated as "react" at read time).
        AgentConfigRequest config = new AgentConfigRequest(
                "prompt", "openai", "gpt-4o", 0.7, List.of(), null, null, null, null,
                "supervisor", null, null, null); // incoming topology = supervisor
        AgentUpdateRequest request = new AgentUpdateRequest("Agent", config, "active", null, null, null);

        doNothing().when(configValidationHelper).validateAgentConfig(any());

        Map<String, Object> existingRow = buildAgentRow("test-agent", "Agent", "active");
        // agent_config has no topology key → defaults to "react"
        when(agentRepository.findByIdAndTenant(TENANT_ID, "test-agent"))
                .thenReturn(Optional.of(existingRow));

        ValidationException ex = assertThrows(ValidationException.class,
                () -> agentService.updateAgent("test-agent", request));
        assertEquals("topology is immutable after agent creation", ex.getMessage(),
                "immutability rejection must carry the exact required message");
    }

    /**
     * PUT changing topology supervisor → react must also be rejected (immutability is
     * bidirectional — a supervisor agent cannot be downgraded to react either).
     */
    @Test
    void updateAgent_topologyChange_supervisorToReact_throws() {
        // Existing row has topology = "supervisor".
        AgentConfigRequest config = new AgentConfigRequest(
                "prompt", "openai", "gpt-4o", 0.7, List.of(), null, null, null, null,
                "react", null, null, null); // incoming topology = react (explicit)
        AgentUpdateRequest request = new AgentUpdateRequest("Agent", config, "active", null, null, null);

        doNothing().when(configValidationHelper).validateAgentConfig(any());

        Map<String, Object> existingRow = buildAgentRowWithTopology("test-agent", "Agent", "active", "supervisor");
        when(agentRepository.findByIdAndTenant(TENANT_ID, "test-agent"))
                .thenReturn(Optional.of(existingRow));

        ValidationException ex = assertThrows(ValidationException.class,
                () -> agentService.updateAgent("test-agent", request));
        assertEquals("topology is immutable after agent creation", ex.getMessage());
    }

    /**
     * PUT that omits topology against a react agent is NOT a topology change — must succeed.
     * This is the common Console update path: the PUT body carries system_prompt / model
     * changes without specifying topology.
     */
    @Test
    void updateAgent_topologyOmitted_reactAgent_succeeds() {
        AgentConfigRequest config = new AgentConfigRequest(
                "updated prompt", "openai", "gpt-4o", 0.7, List.of(), null, null, null, null,
                null, null, null, null); // topology absent → canonicalises to "react"
        AgentUpdateRequest request = new AgentUpdateRequest("Agent", config, "active", null, null, null);

        doNothing().when(configValidationHelper).validateAgentConfig(any());

        Map<String, Object> existingRow = buildAgentRow("test-agent", "Agent", "active");
        // existing has no topology → "react"; incoming null → "react" — not a change
        when(agentRepository.findByIdAndTenant(TENANT_ID, "test-agent"))
                .thenReturn(Optional.of(existingRow));

        Map<String, Object> updatedRow = buildAgentRow("test-agent", "Agent", "active");
        when(agentRepository.update(eq(TENANT_ID), eq("test-agent"), eq("Agent"), anyString(),
                eq("active"), eq(5), eq(500000L), eq(5000000L)))
                .thenReturn(Optional.of(updatedRow));

        // Must NOT throw — topology omission is not a change.
        assertDoesNotThrow(() -> agentService.updateAgent("test-agent", request));
    }

    /**
     * REGRESSION (user-reported): a PUT that OMITS topology against a SUPERVISOR
     * (Deep Research) agent — the Console's normal update path, which never re-sends
     * the immutable topology/preset/supervisor fields — must SUCCEED (omission is not
     * a topology change) AND must PRESERVE topology/preset/supervisor in the persisted
     * agent_config rather than wiping them.
     *
     * <p>Before the fix the gate canonicalised the absent topology to "react",
     * mismatched the persisted "supervisor", and rejected EVERY edit (including a
     * budget-only change) with "topology is immutable after agent creation".
     */
    @Test
    void updateAgent_topologyOmittedOnSupervisorAgent_succeedsAndPreservesImmutableConfig()
            throws Exception {
        // Console-shaped update: mutable fields only; topology/preset/supervisor absent.
        AgentConfigRequest config = new AgentConfigRequest(
                "updated prompt", "openai", "gpt-4o", 0.7, List.of(), null, null, null, null,
                null, null, null, null);
        // Budget-only-style edit (distinct budget numbers to prove they flow through).
        AgentUpdateRequest request = new AgentUpdateRequest("Agent", config, "active",
                3, 900000L, 9000000L);

        doNothing().when(configValidationHelper).validateAgentConfig(any());

        Map<String, Object> existingRow = buildAgentRowWithSupervisorConfig(
                "test-agent", "Agent", "active");
        when(agentRepository.findByIdAndTenant(TENANT_ID, "test-agent"))
                .thenReturn(Optional.of(existingRow));

        Map<String, Object> updatedRow = buildAgentRowWithSupervisorConfig(
                "test-agent", "Agent", "active");
        ArgumentCaptor<String> jsonCaptor = ArgumentCaptor.forClass(String.class);
        when(agentRepository.update(eq(TENANT_ID), eq("test-agent"), eq("Agent"),
                jsonCaptor.capture(), eq("active"), eq(3), eq(900000L), eq(9000000L)))
                .thenReturn(Optional.of(updatedRow));

        // 1. Must NOT throw — omitting topology is not a topology change.
        assertDoesNotThrow(() -> agentService.updateAgent("test-agent", request));

        // 2. The immutable / read-only fields must survive the round-trip (not be wiped).
        AgentConfigRequest parsed = objectMapper.readValue(jsonCaptor.getValue(),
                AgentConfigRequest.class);
        assertEquals("supervisor", parsed.topology(),
                "topology must be preserved when the update omits it");
        assertEquals("research", parsed.preset(),
                "preset must be preserved when the update omits it");
        assertNotNull(parsed.supervisor(),
                "supervisor sub-object must be preserved when the update omits it");
        assertEquals(Integer.valueOf(5), parsed.supervisor().maxFanoutPerIteration());
        assertEquals("formal_report", parsed.supervisor().writerStyle());
        // The mutable field the user actually edited still lands.
        assertEquals("updated prompt", parsed.systemPrompt());
    }

    /**
     * A Console update of a preset agent omits the preset-injected HIDDEN tools
     * (e.g. {@code dispatch_subagent}, the coding sandbox extras) — the Console only
     * echoes the user-facing allowlist back. canonicalizeConfig rebuilds
     * allowed_tools from scratch and re-admits preset-injected names only when they
     * appear in the request's allowed_tools, so without re-deriving them on update a
     * coding/investigation agent silently LOSES dispatch_subagent on every edit even
     * though the preset is preserved. The update path must re-derive the inherited
     * preset's injected tools.
     */
    @Test
    void updateAgent_codingPresetOmitsHiddenTools_preservesDispatchSubagent()
            throws Exception {
        // Console-shaped update: the user-facing allowlist only — dispatch_subagent
        // (hidden, preset-injected) is NOT echoed back; preset/topology absent.
        AgentConfigRequest config = new AgentConfigRequest(
                "updated prompt", "openai", "gpt-4o", 0.7,
                List.of("plan_write"), null, null, null, null,
                null, null, null, null);
        AgentUpdateRequest request = new AgentUpdateRequest("Agent", config, "active",
                null, null, null);

        doNothing().when(configValidationHelper).validateAgentConfig(any());

        Map<String, Object> existingRow = buildAgentRowWithCodingPreset(
                "test-agent", "Agent", "active");
        when(agentRepository.findByIdAndTenant(TENANT_ID, "test-agent"))
                .thenReturn(Optional.of(existingRow));

        ArgumentCaptor<String> jsonCaptor = ArgumentCaptor.forClass(String.class);
        when(agentRepository.update(eq(TENANT_ID), eq("test-agent"), eq("Agent"),
                jsonCaptor.capture(), eq("active"), eq(5), eq(500000L), eq(5000000L)))
                .thenReturn(Optional.of(existingRow));

        assertDoesNotThrow(() -> agentService.updateAgent("test-agent", request));

        AgentConfigRequest parsed = objectMapper.readValue(jsonCaptor.getValue(),
                AgentConfigRequest.class);
        assertEquals("coding", parsed.preset(),
                "preset must be preserved when the update omits it");
        assertTrue(parsed.allowedTools().contains("dispatch_subagent"),
                "the inherited coding preset's hidden dispatch_subagent must survive a "
                        + "Console update that omits it: " + parsed.allowedTools());
    }

    /**
     * PUT that explicitly sends the same topology as the persisted row must succeed.
     */
    @Test
    void updateAgent_topologyIdentical_supervisor_succeeds() {
        AgentConfigRequest config = new AgentConfigRequest(
                "prompt", "openai", "gpt-4o", 0.7, List.of(), null, null, null, null,
                "supervisor", null, null, null); // same topology as persisted
        AgentUpdateRequest request = new AgentUpdateRequest("Agent", config, "active", null, null, null);

        doNothing().when(configValidationHelper).validateAgentConfig(any());

        Map<String, Object> existingRow = buildAgentRowWithTopology("test-agent", "Agent", "active", "supervisor");
        when(agentRepository.findByIdAndTenant(TENANT_ID, "test-agent"))
                .thenReturn(Optional.of(existingRow));

        Map<String, Object> updatedRow = buildAgentRowWithTopology("test-agent", "Agent", "active", "supervisor");
        when(agentRepository.update(eq(TENANT_ID), eq("test-agent"), eq("Agent"), anyString(),
                eq("active"), eq(5), eq(500000L), eq(5000000L)))
                .thenReturn(Optional.of(updatedRow));

        assertDoesNotThrow(() -> agentService.updateAgent("test-agent", request));
    }

    /**
     * PUT that changes other fields (e.g. system_prompt) but keeps topology must succeed.
     */
    @Test
    void updateAgent_otherFieldsChange_topologyUnchanged_succeeds() {
        AgentConfigRequest config = new AgentConfigRequest(
                "totally new system prompt", "openai", "gpt-4o", 0.5, List.of(), null, null, null, null,
                null, null, null, null); // topology absent → "react"
        AgentUpdateRequest request = new AgentUpdateRequest("Agent", config, "active", null, null, null);

        doNothing().when(configValidationHelper).validateAgentConfig(any());

        Map<String, Object> existingRow = buildAgentRow("test-agent", "Agent", "active");
        when(agentRepository.findByIdAndTenant(TENANT_ID, "test-agent"))
                .thenReturn(Optional.of(existingRow));

        Map<String, Object> updatedRow = buildAgentRow("test-agent", "Agent", "active");
        when(agentRepository.update(eq(TENANT_ID), eq("test-agent"), eq("Agent"), anyString(),
                eq("active"), eq(5), eq(500000L), eq(5000000L)))
                .thenReturn(Optional.of(updatedRow));

        assertDoesNotThrow(() -> agentService.updateAgent("test-agent", request));
    }

    /**
     * Agents created before this task (no topology/preset/supervisor in their persisted JSON)
     * remain readable. A PUT against them with no topology succeeds — no migration needed.
     */
    @Test
    void updateAgent_legacyAgentNoTopology_putWithNoTopology_succeeds() {
        // Legacy agent: agent_config has no topology key at all.
        AgentConfigRequest config = new AgentConfigRequest(
                "prompt", "openai", "gpt-4o", 0.7, List.of(), null, null, null, null,
                null, null, null, null);
        AgentUpdateRequest request = new AgentUpdateRequest("Legacy Agent", config, "active", null, null, null);

        doNothing().when(configValidationHelper).validateAgentConfig(any());

        // buildAgentRow returns JSON with no topology key — the legacy shape.
        Map<String, Object> existingRow = buildAgentRow("legacy-agent", "Legacy Agent", "active");
        when(agentRepository.findByIdAndTenant(TENANT_ID, "legacy-agent"))
                .thenReturn(Optional.of(existingRow));

        Map<String, Object> updatedRow = buildAgentRow("legacy-agent", "Legacy Agent", "active");
        when(agentRepository.update(eq(TENANT_ID), eq("legacy-agent"), eq("Legacy Agent"), anyString(),
                eq("active"), eq(5), eq(500000L), eq(5000000L)))
                .thenReturn(Optional.of(updatedRow));

        assertDoesNotThrow(() -> agentService.updateAgent("legacy-agent", request));
    }

    // --- supervisor sub-object round-trip tests ---

    /**
     * POST with a full supervisor sub-object must persist it verbatim — all five fields
     * round-trip through canonicalizeConfig unchanged.
     */
    @Test
    void createAgent_supervisorSubObject_roundTripsVerbatim() throws Exception {
        com.persistentagent.api.model.request.SupervisorConfigRequest supervisor =
                new com.persistentagent.api.model.request.SupervisorConfigRequest(
                        5, 3, List.of("web_search", "my_docs"), "formal_report", true);
        AgentConfigRequest config = new AgentConfigRequest(
                "prompt", "openai", "gpt-4o", 0.7, List.of(), null, null, null, null,
                "supervisor", "research", supervisor, null);
        AgentCreateRequest request = new AgentCreateRequest("Research Agent", config, null, null, null);

        doNothing().when(configValidationHelper).validateAgentConfig(any());

        Timestamp now = Timestamp.from(Instant.now());
        Map<String, Object> repoResult = new LinkedHashMap<>();
        repoResult.put("created_at", now);
        repoResult.put("updated_at", now);

        // S2: research preset seeds max_concurrent_tasks=2 (per-agent admission — see E6 NOTE)
        // and task_timeout_seconds=14400. The agentRepository.insert call reflects seeded values.
        ArgumentCaptor<String> jsonCaptor = ArgumentCaptor.forClass(String.class);
        when(agentRepository.insert(eq(TENANT_ID), anyString(), eq("Research Agent"),
                jsonCaptor.capture(), eq(2), eq(500000L), eq(5000000L)))
                .thenReturn(repoResult);

        agentService.createAgent(request);

        String persistedJson = jsonCaptor.getValue();
        AgentConfigRequest parsed = objectMapper.readValue(persistedJson, AgentConfigRequest.class);

        assertEquals("supervisor", parsed.topology(),
                "topology must round-trip verbatim: " + persistedJson);
        assertEquals("research", parsed.preset(),
                "preset must round-trip verbatim: " + persistedJson);
        assertNotNull(parsed.supervisor(), "supervisor sub-object must round-trip");
        // Explicit max_fanout_per_iteration=5 in the request overrides the preset's seeded 5 → still 5.
        assertEquals(Integer.valueOf(5), parsed.supervisor().maxFanoutPerIteration());
        assertEquals(Integer.valueOf(3), parsed.supervisor().maxIterations());
        // source_allowlist set explicitly on the request → preserved verbatim.
        assertEquals(List.of("web_search", "my_docs"), parsed.supervisor().sourceAllowlist());
        assertEquals("formal_report", parsed.supervisor().writerStyle());
        assertEquals(Boolean.TRUE, parsed.supervisor().scopeClarificationEnabled());
        // S2: research preset seeds task_timeout_seconds=14400 into agent_config JSONB.
        assertEquals(Integer.valueOf(14400), parsed.taskTimeoutSeconds(),
                "research preset must seed task_timeout_seconds=14400: " + persistedJson);

        // Verify snake_case keys are used in the persisted JSON.
        assertTrue(persistedJson.contains("\"topology\":\"supervisor\""), persistedJson);
        assertTrue(persistedJson.contains("\"preset\":\"research\""), persistedJson);
        assertTrue(persistedJson.contains("\"max_fanout_per_iteration\":5"), persistedJson);
        assertTrue(persistedJson.contains("\"max_iterations\":3"), persistedJson);
        assertTrue(persistedJson.contains("\"source_allowlist\""), persistedJson);
        assertTrue(persistedJson.contains("\"writer_style\":\"formal_report\""), persistedJson);
        assertTrue(persistedJson.contains("\"scope_clarification_enabled\":true"), persistedJson);
        assertTrue(persistedJson.contains("\"task_timeout_seconds\":14400"), persistedJson);
    }

    /**
     * POST with no topology — the persisted JSON must omit the topology key entirely.
     * No default "react" is written into the row (absence stays absent).
     */
    @Test
    void createAgent_topologyAbsent_notWrittenToConfig() throws Exception {
        AgentConfigRequest config = new AgentConfigRequest(
                "prompt", "openai", "gpt-4o", 0.7, List.of(), null, null, null, null,
                null, null, null, null);
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
        assertFalse(persistedJson.contains("\"topology\""),
                "topology key must be absent when request omits it (no silent default): " + persistedJson);
        assertFalse(persistedJson.contains("\"preset\""),
                "preset key must be absent when request omits it: " + persistedJson);
        assertFalse(persistedJson.contains("\"supervisor\""),
                "supervisor key must be absent when request omits it: " + persistedJson);
    }

    // --- canonicalizeTopology helper unit tests ---

    @Test
    void canonicalizeTopology_null_returnsReact() {
        assertEquals("react", AgentService.canonicalizeTopology(null));
    }

    @Test
    void canonicalizeTopology_react_returnsReact() {
        assertEquals("react", AgentService.canonicalizeTopology("react"));
    }

    @Test
    void canonicalizeTopology_supervisor_returnsSupervisor() {
        assertEquals("supervisor", AgentService.canonicalizeTopology("supervisor"));
    }

    // --- helpers ---

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

    private Map<String, Object> buildAgentRowWithTopology(
            String agentId, String displayName, String status, String topology) {
        Map<String, Object> row = new LinkedHashMap<>();
        row.put("agent_id", agentId);
        row.put("display_name", displayName);
        row.put("agent_config", "{\"system_prompt\":\"prompt\",\"provider\":\"openai\",\"model\":\"gpt-4o\","
                + "\"temperature\":0.7,\"allowed_tools\":[],\"topology\":\"" + topology + "\"}");
        row.put("status", status);
        row.put("max_concurrent_tasks", 5);
        row.put("budget_max_per_task", 500000L);
        row.put("budget_max_per_hour", 5000000L);
        row.put("created_at", Timestamp.from(Instant.now()));
        row.put("updated_at", Timestamp.from(Instant.now()));
        return row;
    }

    private Map<String, Object> buildAgentRowWithCodingPreset(
            String agentId, String displayName, String status) {
        Map<String, Object> row = new LinkedHashMap<>();
        row.put("agent_id", agentId);
        row.put("display_name", displayName);
        // A coding-preset agent as persisted after creation: dispatch_subagent (hidden,
        // preset-injected) sits in allowed_tools alongside the user-facing tools.
        row.put("agent_config", "{\"system_prompt\":\"prompt\",\"provider\":\"openai\","
                + "\"model\":\"gpt-4o\",\"temperature\":0.7,"
                + "\"allowed_tools\":[\"plan_write\",\"dispatch_subagent\"],"
                + "\"topology\":\"react\",\"preset\":\"coding\"}");
        row.put("status", status);
        row.put("max_concurrent_tasks", 5);
        row.put("budget_max_per_task", 500000L);
        row.put("budget_max_per_hour", 5000000L);
        row.put("created_at", Timestamp.from(Instant.now()));
        row.put("updated_at", Timestamp.from(Instant.now()));
        return row;
    }

    private Map<String, Object> buildAgentRowWithSupervisorConfig(
            String agentId, String displayName, String status) {
        Map<String, Object> row = new LinkedHashMap<>();
        row.put("agent_id", agentId);
        row.put("display_name", displayName);
        // A research-preset supervisor agent as it sits persisted after creation.
        row.put("agent_config", "{\"system_prompt\":\"prompt\",\"provider\":\"openai\","
                + "\"model\":\"gpt-4o\",\"temperature\":0.7,"
                + "\"allowed_tools\":[\"web_search\",\"read_url\"],"
                + "\"topology\":\"supervisor\",\"preset\":\"research\","
                + "\"supervisor\":{\"max_fanout_per_iteration\":5,\"writer_style\":\"formal_report\"}}");
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
