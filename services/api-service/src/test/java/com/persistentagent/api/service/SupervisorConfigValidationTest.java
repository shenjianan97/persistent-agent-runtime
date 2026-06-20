package com.persistentagent.api.service;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.persistentagent.api.exception.ValidationException;
import com.persistentagent.api.model.request.AgentConfigRequest;
import com.persistentagent.api.model.request.SupervisorConfigRequest;
import com.persistentagent.api.repository.AgentRepository;
import com.persistentagent.api.repository.ModelRepository;
import com.persistentagent.api.repository.ToolServerRepository;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;

import java.util.ArrayList;
import java.util.List;

import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.Mockito.*;

/**
 * Unit tests for the Agent Modes / Supervisor Topology S1 config surface:
 *   - topology enum validation
 *   - SupervisorConfigRequest bounds and enum validation
 *   - Jackson round-trip for topology/preset/supervisor on AgentConfigRequest
 *   - validateAgentConfig propagation
 */
@ExtendWith(MockitoExtension.class)
class SupervisorConfigValidationTest {

    @Mock
    private ModelRepository modelRepository;

    @Mock
    private ToolServerRepository toolServerRepository;

    @Mock
    private AgentRepository agentRepository;

    private ConfigValidationHelper helper;
    private ObjectMapper objectMapper;

    @BeforeEach
    void setUp() {
        helper = new ConfigValidationHelper(
                modelRepository, toolServerRepository, agentRepository, new ObjectMapper(), false);
        objectMapper = new ObjectMapper();
    }

    // -----------------------------------------------------------------------
    // topology enum validation
    // -----------------------------------------------------------------------

    @Test
    void validateTopology_null_ok() {
        // Absent topology is always valid — read-time default is "react".
        AgentConfigRequest config = reactConfig(null);
        when(modelRepository.isModelActive("openai", "gpt-4o")).thenReturn(true);
        assertDoesNotThrow(() -> helper.validateAgentConfig(config));
    }

    @Test
    void validateTopology_react_ok() {
        when(modelRepository.isModelActive("openai", "gpt-4o")).thenReturn(true);
        AgentConfigRequest config = reactConfig("react");
        assertDoesNotThrow(() -> helper.validateAgentConfig(config));
    }

    @Test
    void validateTopology_supervisor_ok() {
        when(modelRepository.isModelActive("openai", "gpt-4o")).thenReturn(true);
        AgentConfigRequest config = reactConfig("supervisor");
        assertDoesNotThrow(() -> helper.validateAgentConfig(config));
    }

    @Test
    void validateTopology_deepResearch_throws() {
        // "deep_research" is not a valid topology — there is no mode field (plan §A0 #3).
        when(modelRepository.isModelActive("openai", "gpt-4o")).thenReturn(true);
        AgentConfigRequest config = reactConfig("deep_research");
        ValidationException ex = assertThrows(ValidationException.class,
                () -> helper.validateAgentConfig(config));
        assertTrue(ex.getMessage().contains("deep_research"),
                "error should mention the invalid value: " + ex.getMessage());
    }

    @Test
    void validateTopology_unknownValue_throws() {
        when(modelRepository.isModelActive("openai", "gpt-4o")).thenReturn(true);
        AgentConfigRequest config = reactConfig("workflow_runner");
        ValidationException ex = assertThrows(ValidationException.class,
                () -> helper.validateAgentConfig(config));
        assertTrue(ex.getMessage().toLowerCase().contains("topology"),
                "error should mention topology: " + ex.getMessage());
    }

    // -----------------------------------------------------------------------
    // validateSupervisorConfig — null / empty
    // -----------------------------------------------------------------------

    @Test
    void validateSupervisorConfig_null_ok() {
        // Absent supervisor sub-object is always valid.
        assertDoesNotThrow(() -> helper.validateSupervisorConfig(null));
    }

    @Test
    void validateSupervisorConfig_allFieldsNull_ok() {
        // All five fields null — no validation needed.
        SupervisorConfigRequest s = new SupervisorConfigRequest(null, null, null, null, null);
        assertDoesNotThrow(() -> helper.validateSupervisorConfig(s));
    }

    // -----------------------------------------------------------------------
    // max_fanout_per_iteration bounds [1, 20]
    // -----------------------------------------------------------------------

    @Test
    void validateSupervisorConfig_maxFanoutAtMin_ok() {
        SupervisorConfigRequest s = new SupervisorConfigRequest(1, null, null, null, null);
        assertDoesNotThrow(() -> helper.validateSupervisorConfig(s));
    }

    @Test
    void validateSupervisorConfig_maxFanoutAtMax_ok() {
        SupervisorConfigRequest s = new SupervisorConfigRequest(20, null, null, null, null);
        assertDoesNotThrow(() -> helper.validateSupervisorConfig(s));
    }

    @Test
    void validateSupervisorConfig_maxFanoutAboveMax_throws() {
        // 21 > 20 → 400.
        SupervisorConfigRequest s = new SupervisorConfigRequest(21, null, null, null, null);
        ValidationException ex = assertThrows(ValidationException.class,
                () -> helper.validateSupervisorConfig(s));
        assertTrue(ex.getMessage().contains("max_fanout_per_iteration"),
                "error must name the field: " + ex.getMessage());
        assertTrue(ex.getMessage().contains("20"),
                "error must name upper bound 20: " + ex.getMessage());
    }

    @Test
    void validateSupervisorConfig_maxFanoutBelowMin_throws() {
        // 0 < 1 → 400.
        SupervisorConfigRequest s = new SupervisorConfigRequest(0, null, null, null, null);
        ValidationException ex = assertThrows(ValidationException.class,
                () -> helper.validateSupervisorConfig(s));
        assertTrue(ex.getMessage().contains("max_fanout_per_iteration"),
                "error must name the field: " + ex.getMessage());
        assertTrue(ex.getMessage().contains("1"),
                "error must name lower bound 1: " + ex.getMessage());
    }

    // -----------------------------------------------------------------------
    // max_iterations bounds [1, 10]
    // -----------------------------------------------------------------------

    @Test
    void validateSupervisorConfig_maxIterationsAtMin_ok() {
        SupervisorConfigRequest s = new SupervisorConfigRequest(null, 1, null, null, null);
        assertDoesNotThrow(() -> helper.validateSupervisorConfig(s));
    }

    @Test
    void validateSupervisorConfig_maxIterationsAtMax_ok() {
        SupervisorConfigRequest s = new SupervisorConfigRequest(null, 10, null, null, null);
        assertDoesNotThrow(() -> helper.validateSupervisorConfig(s));
    }

    @Test
    void validateSupervisorConfig_maxIterationsAboveMax_throws() {
        // 11 > 10 → 400.
        SupervisorConfigRequest s = new SupervisorConfigRequest(null, 11, null, null, null);
        ValidationException ex = assertThrows(ValidationException.class,
                () -> helper.validateSupervisorConfig(s));
        assertTrue(ex.getMessage().contains("max_iterations"),
                "error must name the field: " + ex.getMessage());
        assertTrue(ex.getMessage().contains("10"),
                "error must name upper bound 10: " + ex.getMessage());
    }

    @Test
    void validateSupervisorConfig_maxIterationsBelowMin_throws() {
        // 0 < 1 → 400.
        SupervisorConfigRequest s = new SupervisorConfigRequest(null, 0, null, null, null);
        ValidationException ex = assertThrows(ValidationException.class,
                () -> helper.validateSupervisorConfig(s));
        assertTrue(ex.getMessage().contains("max_iterations"),
                "error must name the field: " + ex.getMessage());
    }

    // -----------------------------------------------------------------------
    // source_allowlist ≤ 50 entries
    // -----------------------------------------------------------------------

    @Test
    void validateSupervisorConfig_sourceAllowlistExactly50_ok() {
        List<String> list = new ArrayList<>();
        for (int i = 0; i < 50; i++) list.add("source_" + i);
        SupervisorConfigRequest s = new SupervisorConfigRequest(null, null, list, null, null);
        assertDoesNotThrow(() -> helper.validateSupervisorConfig(s));
    }

    @Test
    void validateSupervisorConfig_sourceAllowlist51_throws() {
        // 51 > 50 → 400 naming the cap.
        List<String> list = new ArrayList<>();
        for (int i = 0; i < 51; i++) list.add("source_" + i);
        SupervisorConfigRequest s = new SupervisorConfigRequest(null, null, list, null, null);
        ValidationException ex = assertThrows(ValidationException.class,
                () -> helper.validateSupervisorConfig(s));
        assertTrue(ex.getMessage().contains("50"),
                "error must name the 50-entry cap: " + ex.getMessage());
        assertTrue(ex.getMessage().contains("source_allowlist"),
                "error must name the field: " + ex.getMessage());
    }

    @Test
    void validateSupervisorConfig_sourceAllowlistEmpty_ok() {
        // Empty allowlist is valid — no cap exceeded.
        SupervisorConfigRequest s = new SupervisorConfigRequest(null, null, List.of(), null, null);
        assertDoesNotThrow(() -> helper.validateSupervisorConfig(s));
    }

    @Test
    void validateSupervisorConfig_sourceAllowlistContentsNotValidated() {
        // Entry contents are not validated — customers may name tools/stores not yet wired.
        SupervisorConfigRequest s = new SupervisorConfigRequest(
                null, null, List.of("unknown-tool", "not-yet-wired-store"), null, null);
        assertDoesNotThrow(() -> helper.validateSupervisorConfig(s));
    }

    // -----------------------------------------------------------------------
    // writer_style enum {formal_report, annotated_bullets}
    // -----------------------------------------------------------------------

    @Test
    void validateSupervisorConfig_writerStyleFormalReport_ok() {
        SupervisorConfigRequest s = new SupervisorConfigRequest(null, null, null, "formal_report", null);
        assertDoesNotThrow(() -> helper.validateSupervisorConfig(s));
    }

    @Test
    void validateSupervisorConfig_writerStyleAnnotatedBullets_ok() {
        SupervisorConfigRequest s = new SupervisorConfigRequest(null, null, null, "annotated_bullets", null);
        assertDoesNotThrow(() -> helper.validateSupervisorConfig(s));
    }

    @Test
    void validateSupervisorConfig_writerStyleInvalid_throws() {
        // "bullet_points" is not a valid writer_style.
        SupervisorConfigRequest s = new SupervisorConfigRequest(null, null, null, "bullet_points", null);
        ValidationException ex = assertThrows(ValidationException.class,
                () -> helper.validateSupervisorConfig(s));
        assertTrue(ex.getMessage().contains("bullet_points"),
                "error must mention the invalid value: " + ex.getMessage());
        assertTrue(ex.getMessage().contains("writer_style"),
                "error must name the field: " + ex.getMessage());
    }

    // -----------------------------------------------------------------------
    // scope_clarification_enabled (boolean toggle — no further validation)
    // -----------------------------------------------------------------------

    @Test
    void validateSupervisorConfig_scopeClarificationEnabledTrue_ok() {
        SupervisorConfigRequest s = new SupervisorConfigRequest(null, null, null, null, true);
        assertDoesNotThrow(() -> helper.validateSupervisorConfig(s));
    }

    @Test
    void validateSupervisorConfig_scopeClarificationEnabledFalse_ok() {
        SupervisorConfigRequest s = new SupervisorConfigRequest(null, null, null, null, false);
        assertDoesNotThrow(() -> helper.validateSupervisorConfig(s));
    }

    // -----------------------------------------------------------------------
    // Full supervisor sub-object (all fields valid)
    // -----------------------------------------------------------------------

    @Test
    void validateSupervisorConfig_allFieldsValid_ok() {
        List<String> allowlist = List.of("web_search", "document_store");
        SupervisorConfigRequest s = new SupervisorConfigRequest(5, 3, allowlist, "formal_report", true);
        assertDoesNotThrow(() -> helper.validateSupervisorConfig(s));
    }

    // -----------------------------------------------------------------------
    // validateAgentConfig propagation — supervisor sub-object errors surface
    // -----------------------------------------------------------------------

    @Test
    void validateAgentConfig_withInvalidSupervisorMaxFanout_throws() {
        // The top-level validateAgentConfig must invoke validateSupervisorConfig.
        when(modelRepository.isModelActive("openai", "gpt-4o")).thenReturn(true);
        SupervisorConfigRequest s = new SupervisorConfigRequest(25, null, null, null, null); // 25 > 20
        AgentConfigRequest config = new AgentConfigRequest(
                "prompt", "openai", "gpt-4o", 0.7, List.of(), null, null, null, null, "supervisor", null, s, null);
        assertThrows(ValidationException.class, () -> helper.validateAgentConfig(config),
                "validateAgentConfig must propagate supervisor validation errors");
    }

    @Test
    void validateAgentConfig_supervisorSubObjectOnReactAgent_accepted() {
        // A supervisor sub-object on a react agent is accepted but inert (no cross-field check).
        when(modelRepository.isModelActive("openai", "gpt-4o")).thenReturn(true);
        SupervisorConfigRequest s = new SupervisorConfigRequest(5, 3, null, "formal_report", true);
        AgentConfigRequest config = new AgentConfigRequest(
                "prompt", "openai", "gpt-4o", 0.7, List.of(), null, null, null, null, "react", null, s, null);
        assertDoesNotThrow(() -> helper.validateAgentConfig(config));
    }

    // -----------------------------------------------------------------------
    // Jackson round-trip: topology, preset, supervisor in AgentConfigRequest
    // -----------------------------------------------------------------------

    @Test
    void agentConfig_jacksonRoundTrip_topologyPreserved() throws Exception {
        // topology must round-trip with snake_case JSON key.
        String json = "{\"system_prompt\":\"p\",\"provider\":\"openai\",\"model\":\"gpt-4o\","
                + "\"temperature\":0.7,\"allowed_tools\":[],\"topology\":\"supervisor\"}";
        AgentConfigRequest parsed = objectMapper.readValue(json, AgentConfigRequest.class);
        assertEquals("supervisor", parsed.topology(),
                "topology must survive Jackson round-trip");
        String serialized = objectMapper.writeValueAsString(parsed);
        assertTrue(serialized.contains("\"topology\":\"supervisor\""),
                "topology must be serialized with its snake_case key: " + serialized);
    }

    @Test
    void agentConfig_jacksonRoundTrip_presetPreserved() throws Exception {
        String json = "{\"system_prompt\":\"p\",\"provider\":\"openai\",\"model\":\"gpt-4o\","
                + "\"temperature\":0.7,\"allowed_tools\":[],\"preset\":\"research\"}";
        AgentConfigRequest parsed = objectMapper.readValue(json, AgentConfigRequest.class);
        assertEquals("research", parsed.preset(),
                "preset must survive Jackson round-trip");
        String serialized = objectMapper.writeValueAsString(parsed);
        assertTrue(serialized.contains("\"preset\":\"research\""),
                "preset must be serialized: " + serialized);
    }

    @Test
    void agentConfig_jacksonRoundTrip_supervisorAllFieldsPreserved() throws Exception {
        String json = "{\"system_prompt\":\"p\",\"provider\":\"openai\",\"model\":\"gpt-4o\","
                + "\"temperature\":0.7,\"allowed_tools\":[],"
                + "\"supervisor\":{"
                + "\"max_fanout_per_iteration\":5,"
                + "\"max_iterations\":3,"
                + "\"source_allowlist\":[\"web_search\",\"my_docs\"],"
                + "\"writer_style\":\"annotated_bullets\","
                + "\"scope_clarification_enabled\":false"
                + "}}";
        AgentConfigRequest parsed = objectMapper.readValue(json, AgentConfigRequest.class);
        assertNotNull(parsed.supervisor(), "supervisor sub-object must survive Jackson round-trip");
        assertEquals(Integer.valueOf(5), parsed.supervisor().maxFanoutPerIteration());
        assertEquals(Integer.valueOf(3), parsed.supervisor().maxIterations());
        assertEquals(List.of("web_search", "my_docs"), parsed.supervisor().sourceAllowlist());
        assertEquals("annotated_bullets", parsed.supervisor().writerStyle());
        assertEquals(Boolean.FALSE, parsed.supervisor().scopeClarificationEnabled());

        // Verify snake_case keys in serialized JSON.
        String serialized = objectMapper.writeValueAsString(parsed);
        assertTrue(serialized.contains("\"max_fanout_per_iteration\":5"),
                "max_fanout_per_iteration must use snake_case: " + serialized);
        assertTrue(serialized.contains("\"max_iterations\":3"),
                "max_iterations must use snake_case: " + serialized);
        assertTrue(serialized.contains("\"source_allowlist\""),
                "source_allowlist must use snake_case: " + serialized);
        assertTrue(serialized.contains("\"writer_style\":\"annotated_bullets\""),
                "writer_style must use snake_case: " + serialized);
        assertTrue(serialized.contains("\"scope_clarification_enabled\":false"),
                "scope_clarification_enabled must use snake_case: " + serialized);
    }

    @Test
    void agentConfig_jacksonRoundTrip_topologyAbsent_keyOmitted() throws Exception {
        // When topology is absent, the serialized JSON must omit the key entirely.
        String json = "{\"system_prompt\":\"p\",\"provider\":\"openai\",\"model\":\"gpt-4o\","
                + "\"temperature\":0.7,\"allowed_tools\":[]}";
        AgentConfigRequest parsed = objectMapper.readValue(json, AgentConfigRequest.class);
        assertNull(parsed.topology());
        String serialized = objectMapper.writeValueAsString(parsed);
        assertFalse(serialized.contains("\"topology\""),
                "topology key must be omitted when absent: " + serialized);
    }

    @Test
    void agentConfig_jacksonRoundTrip_supervisorAbsent_keyOmitted() throws Exception {
        // When supervisor sub-object is absent, the serialized JSON must omit the key.
        String json = "{\"system_prompt\":\"p\",\"provider\":\"openai\",\"model\":\"gpt-4o\","
                + "\"temperature\":0.7,\"allowed_tools\":[]}";
        AgentConfigRequest parsed = objectMapper.readValue(json, AgentConfigRequest.class);
        assertNull(parsed.supervisor());
        String serialized = objectMapper.writeValueAsString(parsed);
        assertFalse(serialized.contains("\"supervisor\""),
                "supervisor key must be omitted when absent: " + serialized);
    }

    @Test
    void supervisorConfig_jacksonDeserialization_acceptsSnakeCaseKeys() throws Exception {
        // SupervisorConfigRequest can be deserialized from the snake_case JSON keys stored in the DB.
        String json = "{\"max_fanout_per_iteration\":10,\"max_iterations\":5,"
                + "\"source_allowlist\":[\"web\"],\"writer_style\":\"formal_report\","
                + "\"scope_clarification_enabled\":true}";
        SupervisorConfigRequest parsed = objectMapper.readValue(json, SupervisorConfigRequest.class);
        assertEquals(Integer.valueOf(10), parsed.maxFanoutPerIteration());
        assertEquals(Integer.valueOf(5), parsed.maxIterations());
        assertEquals(List.of("web"), parsed.sourceAllowlist());
        assertEquals("formal_report", parsed.writerStyle());
        assertEquals(Boolean.TRUE, parsed.scopeClarificationEnabled());
    }

    // -----------------------------------------------------------------------
    // ValidationConstants pins
    // -----------------------------------------------------------------------

    @Test
    void validationConstants_validTopologiesContainReactAndSupervisor() {
        assertTrue(com.persistentagent.api.config.ValidationConstants.VALID_TOPOLOGIES.contains("react"),
                "VALID_TOPOLOGIES must contain 'react'");
        assertTrue(com.persistentagent.api.config.ValidationConstants.VALID_TOPOLOGIES.contains("supervisor"),
                "VALID_TOPOLOGIES must contain 'supervisor'");
        assertFalse(com.persistentagent.api.config.ValidationConstants.VALID_TOPOLOGIES.contains("deep_research"),
                "VALID_TOPOLOGIES must not contain 'deep_research' (no mode field — §A0 #3)");
    }

    @Test
    void validationConstants_validWriterStylesContainExpectedValues() {
        assertTrue(com.persistentagent.api.config.ValidationConstants.VALID_WRITER_STYLES.contains("formal_report"));
        assertTrue(com.persistentagent.api.config.ValidationConstants.VALID_WRITER_STYLES.contains("annotated_bullets"));
        assertEquals(2, com.persistentagent.api.config.ValidationConstants.VALID_WRITER_STYLES.size(),
                "VALID_WRITER_STYLES should have exactly 2 values");
    }

    // -----------------------------------------------------------------------
    // Helpers
    // -----------------------------------------------------------------------

    /**
     * Build a minimal valid AgentConfigRequest with the given topology (may be null)
     * and no supervisor sub-object.
     */
    private AgentConfigRequest reactConfig(String topology) {
        return new AgentConfigRequest(
                "system prompt", "openai", "gpt-4o", 0.7, List.of(), null, null, null, null,
                topology, null, null, null);
    }
}
