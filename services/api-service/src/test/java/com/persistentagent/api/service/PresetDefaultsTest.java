package com.persistentagent.api.service;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.SerializationFeature;
import com.fasterxml.jackson.datatype.jsr310.JavaTimeModule;
import com.persistentagent.api.config.ValidationConstants;
import com.persistentagent.api.exception.ValidationException;
import com.persistentagent.api.model.request.AgentConfigRequest;
import com.persistentagent.api.model.request.AgentCreateRequest;
import com.persistentagent.api.model.request.SandboxConfigRequest;
import com.persistentagent.api.model.request.SupervisorConfigRequest;
import com.persistentagent.api.repository.AgentRepository;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.ArgumentCaptor;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;

import java.sql.Timestamp;
import java.time.Instant;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.ArgumentMatchers.*;
import static org.mockito.Mockito.*;

/**
 * Unit tests for Agent Modes — Supervisor Topology Task S2: PresetDefaults.
 *
 * <p>Covers:
 * <ul>
 *   <li>Each preset's seeded bundle (topology, tools, concurrency/budget, supervisor sub-object,
 *       task_timeout_seconds)</li>
 *   <li>Override rule: explicit request field wins over preset default (per sub-field and per
 *       request-level column)</li>
 *   <li>Unknown preset → 400</li>
 *   <li>research → max_concurrent_tasks=2 on the agents row</li>
 *   <li>Explicit topology-vs-preset contradiction → 400</li>
 *   <li>No preset → no seeding (DEFAULT_* behavior preserved)</li>
 *   <li>No re-seeding on PUT (topology immutability still holds)</li>
 *   <li>workflow_runner → declared placeholder, no Workflow machinery</li>
 * </ul>
 */
@ExtendWith(MockitoExtension.class)
class PresetDefaultsTest {

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
        agentService = new AgentService(agentRepository, configValidationHelper, objectMapper, false);
    }

    // -----------------------------------------------------------------------
    // KNOWN_PRESETS set
    // -----------------------------------------------------------------------

    @Test
    void knownPresets_containsAllFive() {
        assertTrue(PresetDefaults.KNOWN_PRESETS.contains("chat"), "chat must be known");
        assertTrue(PresetDefaults.KNOWN_PRESETS.contains("coding"), "coding must be known");
        assertTrue(PresetDefaults.KNOWN_PRESETS.contains("investigation"), "investigation must be known");
        assertTrue(PresetDefaults.KNOWN_PRESETS.contains("research"), "research must be known");
        assertTrue(PresetDefaults.KNOWN_PRESETS.contains("workflow_runner"), "workflow_runner must be known");
        assertEquals(5, PresetDefaults.KNOWN_PRESETS.size(), "exactly 5 known presets");
    }

    // -----------------------------------------------------------------------
    // no preset → no seeding (pre-S2 behavior preserved)
    // -----------------------------------------------------------------------

    @Test
    void noPreset_noSeeding_defaultsUnchanged() {
        AgentConfigRequest config = minimalConfig(null, null, null);
        AgentCreateRequest request = new AgentCreateRequest("Agent", config, null, null, null);

        AgentCreateRequest result = PresetDefaults.applyPreset(request);

        // No seeding — request unchanged.
        assertSame(request, result, "applyPreset must return the same request when no preset");
        assertNull(result.maxConcurrentTasks(), "max_concurrent_tasks must be unchanged (null)");
        assertNull(result.budgetMaxPerTask(), "budget_max_per_task must be unchanged (null)");
        assertNull(result.budgetMaxPerHour(), "budget_max_per_hour must be unchanged (null)");
    }

    @Test
    void noPreset_agentService_usesDefaultMaxConcurrentTasks() throws Exception {
        // Without a preset, createAgent falls back to DEFAULT_MAX_CONCURRENT_TASKS = 5.
        AgentConfigRequest config = minimalConfig(null, null, null);
        AgentCreateRequest request = new AgentCreateRequest("Agent", config, null, null, null);

        doNothing().when(configValidationHelper).validateAgentConfig(any());

        Timestamp now = Timestamp.from(Instant.now());
        Map<String, Object> repoResult = new LinkedHashMap<>();
        repoResult.put("created_at", now);
        repoResult.put("updated_at", now);
        when(agentRepository.insert(eq(TENANT_ID), anyString(), eq("Agent"), anyString(),
                eq(5), eq(500000L), eq(5000000L)))
                .thenReturn(repoResult);

        agentService.createAgent(request);

        verify(agentRepository).insert(eq(TENANT_ID), anyString(), eq("Agent"), anyString(),
                eq(5), eq(500000L), eq(5000000L));
    }

    @Test
    void noPreset_canonicalizedToolsEqualBaseToolsExactly() throws Exception {
        // PINS the no-preset canonicalization contract (must stay byte-for-byte identical to
        // pre-S2, commit e61337d): with no preset and no sandbox, the persisted allowed_tools
        // is EXACTLY BASE_PLATFORM_TOOLS — no more, no less.
        AgentConfigRequest config = minimalConfig(null, null, null);
        List<String> persistedTools = createAgentAndCapturePersistedTools(config);

        assertEquals(ValidationConstants.BASE_PLATFORM_TOOLS, persistedTools,
                "no-preset, no-sandbox agent must persist exactly BASE_PLATFORM_TOOLS; got: " + persistedTools);
    }

    @Test
    void noPreset_customerSuppliedNonBaseToolIsDropped() throws Exception {
        // PINS that the no-preset path still DROPS a caller-supplied tool that the derivation
        // does not produce — exactly as pre-S2 (e61337d). canonicalizeConfig rebuilds the tool
        // list from config flags; it does not echo back arbitrary allowed_tools entries.
        // (In production validateAllowedTools would let sandbox_exec pass — it is in
        // ALLOWED_TOOLS — but canonicalize drops it because sandbox.enabled is unset. This test
        // stubs out validation to isolate canonicalizeConfig's dropping behavior.)
        //
        // sandbox_exec is deliberately chosen because it IS in PresetDefaults.PRESET_INJECTED_TOOLS
        // (via CODING_EXTRA_TOOLS): it would survive canonicalization if the carry-through loop
        // weren't gated on config.preset() != null. With no preset here, the gate keeps it out —
        // this is the regression guard for that gate.
        AgentConfigRequest config = new AgentConfigRequest(
                "system prompt", "openai", "gpt-4o", 0.7,
                List.of("sandbox_exec"), // caller lists a sandbox tool but sandbox is NOT enabled
                null, null, null, null, null, null, null, null);

        List<String> persistedTools = createAgentAndCapturePersistedTools(config);

        assertFalse(persistedTools.contains("sandbox_exec"),
                "no-preset path must DROP a sandbox tool when sandbox.enabled is not set "
                + "(pre-S2 derivation behavior); got: " + persistedTools);
        assertEquals(ValidationConstants.BASE_PLATFORM_TOOLS, persistedTools,
                "no-preset path must persist exactly BASE_PLATFORM_TOOLS; got: " + persistedTools);
    }

    @Test
    void noPreset_sandboxEnabled_addsSandboxToolsOnly() throws Exception {
        // PINS the no-preset + sandbox-enabled derivation: BASE + SANDBOX tools, nothing else.
        SandboxConfigRequest sandbox = new SandboxConfigRequest(true, null, null, null, null);
        AgentConfigRequest config = new AgentConfigRequest(
                "system prompt", "openai", "gpt-4o", 0.7,
                null, null, sandbox, null, null, null, null, null, null);

        List<String> persistedTools = createAgentAndCapturePersistedTools(config);

        for (String base : ValidationConstants.BASE_PLATFORM_TOOLS) {
            assertTrue(persistedTools.contains(base), "must contain base tool " + base);
        }
        for (String sb : ValidationConstants.SANDBOX_TOOLS) {
            assertTrue(persistedTools.contains(sb), "must contain sandbox tool " + sb);
        }
        assertFalse(persistedTools.contains("dispatch_subagent"),
                "no-preset path must never inject preset-only tools; got: " + persistedTools);
        assertEquals(ValidationConstants.BASE_PLATFORM_TOOLS.size() + ValidationConstants.SANDBOX_TOOLS.size(),
                persistedTools.size(),
                "no-preset + sandbox must persist exactly BASE + SANDBOX (no extras); got: " + persistedTools);
    }

    // -----------------------------------------------------------------------
    // chat preset
    // -----------------------------------------------------------------------

    @Test
    void chatPreset_seedsTopologyReact() {
        AgentCreateRequest result = applyPreset("chat");
        assertEquals("react", result.agentConfig().topology(),
                "chat must seed topology=react");
    }

    @Test
    void chatPreset_seedsSmallBudget() {
        AgentCreateRequest result = applyPreset("chat");
        assertEquals(PresetDefaults.CHAT_BUDGET_MAX_PER_TASK, result.budgetMaxPerTask(),
                "chat must seed a smaller per-task budget than the platform default");
        assertTrue(result.budgetMaxPerTask() < ValidationConstants.DEFAULT_BUDGET_MAX_PER_TASK,
                "chat budget must be less than the platform default");
    }

    @Test
    void chatPreset_noSupervisorSubObject() {
        AgentCreateRequest result = applyPreset("chat");
        assertNull(result.agentConfig().supervisor(),
                "chat must not seed a supervisor sub-object");
    }

    @Test
    void chatPreset_noTaskTimeoutSeeded() {
        AgentCreateRequest result = applyPreset("chat");
        assertNull(result.agentConfig().taskTimeoutSeconds(),
                "chat must not seed task_timeout_seconds (uses platform default 3600 at submission)");
    }

    @Test
    void chatPreset_noExtraToolsSeeded() {
        // chat leaves tools customer-defined; no extra tools beyond the base platform tools
        // that canonicalizeConfig always adds.
        AgentCreateRequest result = applyPreset("chat");
        // allowed_tools on the config may be null or empty — chat does not add extra tools.
        List<String> tools = result.agentConfig().allowedTools();
        if (tools != null) {
            assertFalse(tools.contains("dispatch_subagent"),
                    "chat must not seed dispatch_subagent");
            assertFalse(tools.contains("sandbox_exec"),
                    "chat must not seed sandbox tools");
        }
    }

    // -----------------------------------------------------------------------
    // coding preset
    // -----------------------------------------------------------------------

    @Test
    void codingPreset_seedsTopologyReact() {
        AgentCreateRequest result = applyPreset("coding");
        assertEquals("react", result.agentConfig().topology(),
                "coding must seed topology=react");
    }

    @Test
    void codingPreset_seedsDispatchSubagentInAllowedTools() {
        AgentCreateRequest result = applyPreset("coding");
        List<String> tools = result.agentConfig().allowedTools();
        assertNotNull(tools, "allowed_tools must be non-null after coding preset");
        assertTrue(tools.contains("dispatch_subagent"),
                "coding preset must seed dispatch_subagent in the tool allowlist "
                + "(Track-7 precedent: seeded ahead of S4 runtime delivery); got: " + tools);
    }

    @Test
    void codingPreset_seedsSandboxToolsInAllowedTools() {
        AgentCreateRequest result = applyPreset("coding");
        List<String> tools = result.agentConfig().allowedTools();
        assertNotNull(tools);
        assertTrue(tools.contains("sandbox_exec"),
                "coding preset must seed sandbox_exec; got: " + tools);
        assertTrue(tools.contains("sandbox_read_file"),
                "coding preset must seed sandbox_read_file; got: " + tools);
        assertTrue(tools.contains("sandbox_write_file"),
                "coding preset must seed sandbox_write_file; got: " + tools);
        assertTrue(tools.contains("export_sandbox_file"),
                "coding preset must seed export_sandbox_file; got: " + tools);
    }

    @Test
    void codingPreset_seedsLargerBudgetThanChat() {
        AgentCreateRequest coding = applyPreset("coding");
        AgentCreateRequest chat = applyPreset("chat");
        assertTrue(coding.budgetMaxPerTask() > chat.budgetMaxPerTask(),
                "coding must have a larger per-task budget than chat");
        assertTrue(coding.budgetMaxPerTask() > ValidationConstants.DEFAULT_BUDGET_MAX_PER_TASK,
                "coding budget must exceed the platform default");
    }

    @Test
    void codingPreset_noSupervisorSubObject() {
        AgentCreateRequest result = applyPreset("coding");
        assertNull(result.agentConfig().supervisor(),
                "coding must not seed a supervisor sub-object");
    }

    // -----------------------------------------------------------------------
    // investigation preset
    // -----------------------------------------------------------------------

    @Test
    void investigationPreset_seedsTopologyReact() {
        AgentCreateRequest result = applyPreset("investigation");
        assertEquals("react", result.agentConfig().topology(),
                "investigation must seed topology=react");
    }

    @Test
    void investigationPreset_seedsDispatchSubagentInAllowedTools() {
        AgentCreateRequest result = applyPreset("investigation");
        List<String> tools = result.agentConfig().allowedTools();
        assertNotNull(tools, "allowed_tools must be non-null after investigation preset");
        assertTrue(tools.contains("dispatch_subagent"),
                "investigation preset must seed dispatch_subagent; got: " + tools);
    }

    @Test
    void investigationPreset_noSupervisorSubObject() {
        AgentCreateRequest result = applyPreset("investigation");
        assertNull(result.agentConfig().supervisor(),
                "investigation must not seed a supervisor sub-object");
    }

    // -----------------------------------------------------------------------
    // research preset — design-pinned values (load-bearing)
    // -----------------------------------------------------------------------

    @Test
    void researchPreset_seedsTopologySupervisor() {
        AgentCreateRequest result = applyPreset("research");
        assertEquals("supervisor", result.agentConfig().topology(),
                "research preset must seed topology=supervisor (design-pinned)");
    }

    @Test
    void researchPreset_seedsMaxFanoutPerIteration5() {
        AgentCreateRequest result = applyPreset("research");
        assertNotNull(result.agentConfig().supervisor(), "research must seed a supervisor sub-object");
        assertEquals(Integer.valueOf(5), result.agentConfig().supervisor().maxFanoutPerIteration(),
                "research preset must seed max_fanout_per_iteration=5 (design-pinned)");
    }

    @Test
    void researchPreset_seedsWriterStyleFormalReport() {
        AgentCreateRequest result = applyPreset("research");
        assertNotNull(result.agentConfig().supervisor());
        assertEquals("formal_report", result.agentConfig().supervisor().writerStyle(),
                "research preset must seed writer_style=formal_report (design-pinned)");
    }

    @Test
    void researchPreset_seedsMaxConcurrentTasks2() {
        // E6 NOTE (plan §A6.4): max_concurrent_tasks=2 is per-AGENT admission (agents.max_concurrent_tasks),
        // NOT a cross-tenant worker-slot guard. The real mitigation for cross-tenant starvation is
        // worker-pool sizing / isolation (ops — §A6.4 / §A11-E6).
        AgentCreateRequest result = applyPreset("research");
        assertEquals(Integer.valueOf(2), result.maxConcurrentTasks(),
                "research preset must seed max_concurrent_tasks=2 (per-agent admission, design-pinned)");
    }

    @Test
    void researchPreset_seedsTaskTimeoutSeconds14400() {
        // E7 NOTE (plan §A6.5): whole Deep Research run is one task; seeded in agent_config JSONB.
        // The 14400 s (4 h) default is sized from worst-case fan-out wall-clock (see PresetDefaults Javadoc).
        AgentCreateRequest result = applyPreset("research");
        assertEquals(Integer.valueOf(14400), result.agentConfig().taskTimeoutSeconds(),
                "research preset must seed task_timeout_seconds=14400 (4 h, agent_config JSONB, E7)");
    }

    @Test
    void researchPreset_taskTimeoutSeededNotInsideSupervisor() {
        // task_timeout_seconds is a per-task/agent-default field, NOT inside the supervisor sub-object.
        AgentCreateRequest result = applyPreset("research");
        assertNotNull(result.agentConfig().taskTimeoutSeconds(),
                "task_timeout_seconds must be on agent_config, not in supervisor");
        // supervisor sub-object does not have a taskTimeoutSeconds field — its fields are
        // max_fanout_per_iteration, max_iterations, source_allowlist, writer_style,
        // scope_clarification_enabled. The test below confirms the supervisor sub-object is
        // correctly seeded and is separate from task_timeout_seconds.
        assertNotNull(result.agentConfig().supervisor());
    }

    @Test
    void researchPreset_seedsWebToolsInAllowedTools() {
        // web_search and read_url are the research preset's tool allowlist.
        // Verified against worker tool registry: tools/definitions.py lines 165 and 171.
        AgentCreateRequest result = applyPreset("research");
        List<String> tools = result.agentConfig().allowedTools();
        assertNotNull(tools, "allowed_tools must be non-null after research preset");
        assertTrue(tools.contains("web_search"),
                "research preset must seed web_search; got: " + tools);
        assertTrue(tools.contains("read_url"),
                "research preset must seed read_url; got: " + tools);
    }

    @Test
    void researchPreset_supervisorSubObjectPassesS1Bounds() {
        // Seeded supervisor sub-object must satisfy S1's validator bounds/enums.
        AgentCreateRequest result = applyPreset("research");
        SupervisorConfigRequest sup = result.agentConfig().supervisor();
        assertNotNull(sup);
        // max_fanout_per_iteration=5 ∈ [1, 20] ✓
        assertTrue(sup.maxFanoutPerIteration() >= ValidationConstants.SUPERVISOR_MAX_FANOUT_MIN
                        && sup.maxFanoutPerIteration() <= ValidationConstants.SUPERVISOR_MAX_FANOUT_MAX,
                "seeded max_fanout_per_iteration=5 must be within [1,20]");
        // writer_style=formal_report ∈ {formal_report, annotated_bullets} ✓
        assertTrue(ValidationConstants.VALID_WRITER_STYLES.contains(sup.writerStyle()),
                "seeded writer_style=formal_report must be a valid VALID_WRITER_STYLES value");
    }

    // -----------------------------------------------------------------------
    // research preset via createAgent (integration of seeding + canonical + row)
    // -----------------------------------------------------------------------

    @Test
    void researchPreset_createAgent_maxConcurrentTasksIsTwo_onAgentsRow() throws Exception {
        // The main acceptance criterion: POST /v1/agents with preset=research results in
        // max_concurrent_tasks=2 on the agents row (not just in agent_config).
        AgentConfigRequest config = minimalConfig("supervisor", "research",
                new SupervisorConfigRequest(5, null, null, "formal_report", null));
        AgentCreateRequest request = new AgentCreateRequest("Research Agent", config, null, null, null);

        doNothing().when(configValidationHelper).validateAgentConfig(any());

        Timestamp now = Timestamp.from(Instant.now());
        Map<String, Object> repoResult = new LinkedHashMap<>();
        repoResult.put("created_at", now);
        repoResult.put("updated_at", now);
        // research preset seeds max_concurrent_tasks=2 and budget defaults.
        ArgumentCaptor<Integer> concurrencyCaptor = ArgumentCaptor.forClass(Integer.class);
        when(agentRepository.insert(eq(TENANT_ID), anyString(), eq("Research Agent"), anyString(),
                concurrencyCaptor.capture(), anyLong(), anyLong()))
                .thenReturn(repoResult);

        agentService.createAgent(request);

        assertEquals(2, concurrencyCaptor.getValue(),
                "research preset must produce max_concurrent_tasks=2 on the agents row (E6 per-agent admission)");
    }

    @Test
    void researchPreset_createAgent_taskTimeoutSeededInJson() throws Exception {
        AgentConfigRequest config = minimalConfig("supervisor", "research", null);
        AgentCreateRequest request = new AgentCreateRequest("Research Agent", config, null, null, null);

        doNothing().when(configValidationHelper).validateAgentConfig(any());

        Timestamp now = Timestamp.from(Instant.now());
        Map<String, Object> repoResult = new LinkedHashMap<>();
        repoResult.put("created_at", now);
        repoResult.put("updated_at", now);

        ArgumentCaptor<String> jsonCaptor = ArgumentCaptor.forClass(String.class);
        when(agentRepository.insert(eq(TENANT_ID), anyString(), eq("Research Agent"),
                jsonCaptor.capture(), eq(2), eq(500000L), eq(5000000L)))
                .thenReturn(repoResult);

        agentService.createAgent(request);

        String persistedJson = jsonCaptor.getValue();
        AgentConfigRequest parsed = objectMapper.readValue(persistedJson, AgentConfigRequest.class);
        assertEquals(Integer.valueOf(14400), parsed.taskTimeoutSeconds(),
                "research preset must seed task_timeout_seconds=14400 in agent_config JSONB; got: " + persistedJson);
    }

    // -----------------------------------------------------------------------
    // plan_write reaches preset agents via the BASE_PLATFORM_TOOLS merge — NOT the preset.
    // (2026-06-06 product decision, PR #115: plan_write is a base platform tool. The preset
    // EXTRA_TOOLS lists intentionally do NOT seed it; canonicalizeConfig merges base tools into
    // every agent's allowed_tools. These tests document that interaction and guard against base
    // tools regressing.)
    // -----------------------------------------------------------------------

    @Test
    void codingPreset_planWritePresentViaBaseTools() throws Exception {
        // coding agent's persisted allowed_tools must contain plan_write (via base-tool merge)
        // AND dispatch_subagent (via the preset's EXTRA_TOOLS) — confirming both paths.
        List<String> persistedTools = createAgentAndCapturePersistedTools("coding", "react");

        assertTrue(persistedTools.contains("plan_write"),
                "coding agent's persisted allowed_tools must contain plan_write via the "
                + "BASE_PLATFORM_TOOLS merge (2026-06-06 product decision, PR #115); got: " + persistedTools);
        assertTrue(persistedTools.contains("dispatch_subagent"),
                "coding agent's persisted allowed_tools must contain dispatch_subagent "
                + "(preset EXTRA_TOOLS); got: " + persistedTools);
    }

    @Test
    void investigationPreset_planWritePresentViaBaseTools() throws Exception {
        List<String> persistedTools = createAgentAndCapturePersistedTools("investigation", "react");

        assertTrue(persistedTools.contains("plan_write"),
                "investigation agent's persisted allowed_tools must contain plan_write via the "
                + "BASE_PLATFORM_TOOLS merge (2026-06-06 product decision, PR #115); got: " + persistedTools);
        assertTrue(persistedTools.contains("dispatch_subagent"),
                "investigation agent's persisted allowed_tools must contain dispatch_subagent "
                + "(preset EXTRA_TOOLS); got: " + persistedTools);
    }

    @Test
    void planWriteNotInPresetExtraToolsConstants() {
        // Belt-and-suspenders: the preset EXTRA_TOOLS constants must NOT list plan_write —
        // it is supplied by the base-tool merge, not the preset (2026-06-06 decision, PR #115).
        assertFalse(PresetDefaults.CODING_EXTRA_TOOLS.contains("plan_write"),
                "CODING_EXTRA_TOOLS must not seed plan_write — it is a base platform tool");
        assertFalse(PresetDefaults.INVESTIGATION_EXTRA_TOOLS.contains("plan_write"),
                "INVESTIGATION_EXTRA_TOOLS must not seed plan_write — it is a base platform tool");
        // Sanity: plan_write IS a base platform tool (the source of truth for the merge).
        assertTrue(ValidationConstants.BASE_PLATFORM_TOOLS.contains("plan_write"),
                "BASE_PLATFORM_TOOLS must contain plan_write (the merge source — PR #115)");
    }

    // -----------------------------------------------------------------------
    // workflow_runner preset — declared, not actionable (Phase 3)
    // -----------------------------------------------------------------------

    @Test
    void workflowRunnerPreset_isKnown_doesNotReturn400() {
        // workflow_runner is declared in KNOWN_PRESETS so the validator accepts it.
        // No Workflow machinery is wired (Phase 3).
        assertTrue(PresetDefaults.KNOWN_PRESETS.contains("workflow_runner"),
                "workflow_runner must be in KNOWN_PRESETS (reserved name)");
    }

    @Test
    void workflowRunnerPreset_seedsReactTopology_asPlaceholder() {
        // Decision (S2): workflow_runner seeds react topology as a placeholder.
        // No execute_workflow tool, no workflow_id submission, no step-list machinery.
        AgentCreateRequest result = applyPreset("workflow_runner");
        assertEquals("react", result.agentConfig().topology(),
                "workflow_runner seeds topology=react as a placeholder (Phase 3 deferred)");
    }

    @Test
    void workflowRunnerPreset_noWorkflowMachinery() {
        // Confirms no Workflow-specific tools are seeded.
        AgentCreateRequest result = applyPreset("workflow_runner");
        List<String> tools = result.agentConfig().allowedTools();
        if (tools != null) {
            assertFalse(tools.contains("execute_workflow"),
                    "workflow_runner must NOT seed execute_workflow (Phase 3)");
        }
    }

    // -----------------------------------------------------------------------
    // Unknown preset → 400
    // -----------------------------------------------------------------------

    @Test
    void unknownPreset_validatePreset_throws400() {
        // ConfigValidationHelper.validatePreset rejects unknown preset names with 400.
        ConfigValidationHelper helper = buildHelper();

        ValidationException ex = assertThrows(ValidationException.class,
                () -> helper.validatePreset("bogus_preset", null),
                "unknown preset must be rejected with 400");
        assertTrue(ex.getMessage().contains("bogus_preset"),
                "error message must name the invalid preset: " + ex.getMessage());
        // Must also name the valid presets.
        assertTrue(PresetDefaults.KNOWN_PRESETS.stream()
                        .anyMatch(p -> ex.getMessage().contains(p)),
                "error message must name at least one valid preset: " + ex.getMessage());
    }

    @Test
    void unknownPreset_validatePreset_calledByValidateAgentConfig() {
        // Documents that unknown-preset rejection is wired into ConfigValidationHelper.validatePreset,
        // which is called at the end of validateAgentConfig. Testing the full validateAgentConfig
        // path requires stubbing all other validators (model existence, tool-server lookups, etc.),
        // which is the concern of ConfigValidationHelperTest.
        // Here we confirm the direct validatePreset() path rejects any unrecognized name.
        ConfigValidationHelper helper = buildHelper();
        assertThrows(ValidationException.class,
                () -> helper.validatePreset("totally_unknown", null),
                "validatePreset must reject any unrecognised preset name");
    }

    @Test
    void unknownPreset_directValidatePreset_throwsWithValidPresetList() {
        ConfigValidationHelper helper = buildHelper();
        ValidationException ex = assertThrows(ValidationException.class,
                () -> helper.validatePreset("my_custom_preset", null));
        // The error must name the valid presets so the customer knows their options.
        for (String known : PresetDefaults.KNOWN_PRESETS) {
            assertTrue(ex.getMessage().contains(known),
                    "error must name '" + known + "' in the valid preset list: " + ex.getMessage());
        }
    }

    // -----------------------------------------------------------------------
    // Explicit-topology vs. preset contradiction → 400
    // -----------------------------------------------------------------------

    @Test
    void researchPreset_withExplicitTopologyReact_throws400() {
        // Decision (S2): if request names preset=research (topology=supervisor) AND explicitly
        // sets topology=react, reject 400 (ambiguous, contradictory intent).
        assertThrows(ValidationException.class,
                () -> PresetDefaults.validatePresetTopologyConsistency("research", "react"),
                "preset=research + topology=react must be rejected 400");
    }

    @Test
    void chatPreset_withExplicitTopologySupervisor_throws400() {
        assertThrows(ValidationException.class,
                () -> PresetDefaults.validatePresetTopologyConsistency("chat", "supervisor"),
                "preset=chat + topology=supervisor must be rejected 400");
    }

    @Test
    void researchPreset_withMatchingTopologySupervisor_ok() {
        // topology=supervisor is consistent with preset=research — no contradiction.
        assertDoesNotThrow(
                () -> PresetDefaults.validatePresetTopologyConsistency("research", "supervisor"),
                "preset=research + topology=supervisor is consistent, must not throw");
    }

    @Test
    void researchPreset_withNoExplicitTopology_ok() {
        // topology absent — preset seeds it. No contradiction possible.
        assertDoesNotThrow(
                () -> PresetDefaults.validatePresetTopologyConsistency("research", null),
                "preset=research + topology=null (absent) is valid");
    }

    @Test
    void noPreset_anyTopology_ok() {
        // No preset — no contradiction check.
        assertDoesNotThrow(
                () -> PresetDefaults.validatePresetTopologyConsistency(null, "supervisor"),
                "no preset, any topology — must not throw");
        assertDoesNotThrow(
                () -> PresetDefaults.validatePresetTopologyConsistency(null, null),
                "no preset, no topology — must not throw");
    }

    @Test
    void researchPreset_withExplicitTopologyReact_errorMessageDescribesConflict() {
        ValidationException ex = assertThrows(ValidationException.class,
                () -> PresetDefaults.validatePresetTopologyConsistency("research", "react"));
        assertTrue(ex.getMessage().contains("research"),
                "error must name the preset: " + ex.getMessage());
        assertTrue(ex.getMessage().contains("supervisor"),
                "error must name the preset's topology: " + ex.getMessage());
        assertTrue(ex.getMessage().contains("react"),
                "error must name the conflicting explicit topology: " + ex.getMessage());
    }

    // -----------------------------------------------------------------------
    // Override rule: explicit request field wins over preset default
    // -----------------------------------------------------------------------

    @Test
    void researchPreset_explicitMaxConcurrentTasksOverridesPreset() {
        // Explicit max_concurrent_tasks=4 overrides the preset's 2.
        AgentConfigRequest config = minimalConfig("supervisor", "research", null);
        AgentCreateRequest request = new AgentCreateRequest("Agent", config, 4, null, null);

        AgentCreateRequest result = PresetDefaults.applyPreset(request);

        assertEquals(Integer.valueOf(4), result.maxConcurrentTasks(),
                "explicit max_concurrent_tasks=4 must override preset's 2");
    }

    @Test
    void researchPreset_explicitBudgetOverridesPreset() {
        long explicitBudget = 3_000_000L;
        AgentConfigRequest config = minimalConfig("supervisor", "research", null);
        AgentCreateRequest request = new AgentCreateRequest("Agent", config, null, explicitBudget, null);

        AgentCreateRequest result = PresetDefaults.applyPreset(request);

        assertEquals(explicitBudget, result.budgetMaxPerTask(),
                "explicit budget_max_per_task must override preset default");
    }

    @Test
    void researchPreset_explicitMaxFanoutOverridesPreset() {
        // research preset seeds max_fanout_per_iteration=5; explicit 8 must win.
        SupervisorConfigRequest explicitSupervisor = new SupervisorConfigRequest(8, null, null, null, null);
        AgentConfigRequest config = minimalConfig("supervisor", "research", explicitSupervisor);
        AgentCreateRequest request = new AgentCreateRequest("Agent", config, null, null, null);

        AgentCreateRequest result = PresetDefaults.applyPreset(request);

        assertNotNull(result.agentConfig().supervisor());
        assertEquals(Integer.valueOf(8), result.agentConfig().supervisor().maxFanoutPerIteration(),
                "explicit max_fanout_per_iteration=8 must override preset's 5");
    }

    @Test
    void researchPreset_explicitWriterStyleOverridesPreset() {
        // research preset seeds writer_style=formal_report; explicit annotated_bullets must win.
        SupervisorConfigRequest explicitSupervisor = new SupervisorConfigRequest(null, null, null, "annotated_bullets", null);
        AgentConfigRequest config = minimalConfig("supervisor", "research", explicitSupervisor);
        AgentCreateRequest request = new AgentCreateRequest("Agent", config, null, null, null);

        AgentCreateRequest result = PresetDefaults.applyPreset(request);

        assertEquals("annotated_bullets", result.agentConfig().supervisor().writerStyle(),
                "explicit writer_style=annotated_bullets must override preset's formal_report");
    }

    @Test
    void researchPreset_explicitTaskTimeoutSecondsOverridesPreset() {
        // research preset seeds task_timeout_seconds=14400; explicit 7200 must win.
        AgentConfigRequest config = new AgentConfigRequest(
                "prompt", "openai", "gpt-4o", 0.7, null, null, null, null, null,
                "supervisor", "research", null, 7200);
        AgentCreateRequest request = new AgentCreateRequest("Agent", config, null, null, null);

        AgentCreateRequest result = PresetDefaults.applyPreset(request);

        assertEquals(Integer.valueOf(7200), result.agentConfig().taskTimeoutSeconds(),
                "explicit task_timeout_seconds=7200 must override preset's 14400");
    }

    @Test
    void researchPreset_supervisorSubFieldAbsent_presetDefaultFills() {
        // max_fanout_per_iteration absent → preset fills with 5.
        AgentCreateRequest result = applyPreset("research");
        assertEquals(Integer.valueOf(5), result.agentConfig().supervisor().maxFanoutPerIteration(),
                "absent max_fanout_per_iteration must be filled by preset default 5");
    }

    @Test
    void researchPreset_supervisorMaxIterationsAbsent_presetDoesNotFill() {
        // research preset does not seed max_iterations — it stays null (worker uses its own default).
        AgentCreateRequest result = applyPreset("research");
        assertNull(result.agentConfig().supervisor().maxIterations(),
                "research preset must not seed max_iterations (customer/worker default)");
    }

    @Test
    void codingPreset_explicitBudgetOverridesPreset() {
        long explicitBudget = 5_000_000L;
        AgentConfigRequest config = minimalConfig(null, "coding", null);
        AgentCreateRequest request = new AgentCreateRequest("Agent", config, null, explicitBudget, null);

        AgentCreateRequest result = PresetDefaults.applyPreset(request);

        assertEquals(explicitBudget, result.budgetMaxPerTask(),
                "explicit budget_max_per_task must override coding preset's seeded value");
    }

    // -----------------------------------------------------------------------
    // No re-seeding on PUT (topology immutability test — S1 owns the gate)
    // -----------------------------------------------------------------------

    @Test
    void applyPreset_onlyAppliesAtCreation_notOnUpdate() {
        // The spec requires preset seeding at creation only. In the service layer, applyPreset
        // is called exclusively from createAgent (never from updateAgent). This test documents
        // the boundary: updateAgent does NOT call applyPreset, so even if a preset is
        // named in the agentConfig, no re-seeding occurs.
        //
        // Implementation evidence: PresetDefaults.applyPreset is invoked only in AgentService.createAgent
        // (verified by code inspection). updateAgent reads existing concurrency/budget from the row
        // and applies the request — no preset merge. This test documents the contract.
        // A separate test in AgentServiceTest covers the topology immutability gate itself.

        // Sanity check: applyPreset with a null preset returns the same request (no-op).
        AgentConfigRequest config = minimalConfig(null, null, null);
        AgentCreateRequest request = new AgentCreateRequest("Agent", config, null, null, null);
        assertSame(request, PresetDefaults.applyPreset(request),
                "applyPreset must be a no-op when preset is null");
    }

    // -----------------------------------------------------------------------
    // presetTopology helper
    // -----------------------------------------------------------------------

    @Test
    void presetTopology_research_returnsSupervisor() {
        assertEquals("supervisor", PresetDefaults.presetTopology("research"));
    }

    @Test
    void presetTopology_chat_returnsReact() {
        assertEquals("react", PresetDefaults.presetTopology("chat"));
    }

    @Test
    void presetTopology_coding_returnsReact() {
        assertEquals("react", PresetDefaults.presetTopology("coding"));
    }

    @Test
    void presetTopology_investigation_returnsReact() {
        assertEquals("react", PresetDefaults.presetTopology("investigation"));
    }

    @Test
    void presetTopology_workflowRunner_returnsReact() {
        assertEquals("react", PresetDefaults.presetTopology("workflow_runner"));
    }

    @Test
    void presetTopology_null_returnsNull() {
        assertNull(PresetDefaults.presetTopology(null));
    }

    @Test
    void presetTopology_unknown_returnsNull() {
        assertNull(PresetDefaults.presetTopology("bogus"));
    }

    // -----------------------------------------------------------------------
    // mergeExtraTools helper
    // -----------------------------------------------------------------------

    @Test
    void mergeExtraTools_null_existing_returnsExtraOnly() {
        List<String> result = PresetDefaults.mergeExtraTools(null, List.of("dispatch_subagent"));
        assertTrue(result.contains("dispatch_subagent"));
    }

    @Test
    void mergeExtraTools_noDuplicates() {
        // Extra tool already present in existing → no duplication.
        List<String> existing = new java.util.ArrayList<>(List.of("web_search"));
        List<String> result = PresetDefaults.mergeExtraTools(existing, List.of("web_search", "dispatch_subagent"));
        long count = result.stream().filter("web_search"::equals).count();
        assertEquals(1, count, "web_search must appear exactly once after merge");
        assertTrue(result.contains("dispatch_subagent"));
    }

    @Test
    void mergeExtraTools_emptyExtra_returnsExisting() {
        List<String> existing = List.of("web_search");
        List<String> result = PresetDefaults.mergeExtraTools(existing, List.of());
        assertEquals(existing, result, "empty extra list must return existing unchanged");
    }

    // -----------------------------------------------------------------------
    // firstNonNull / firstNonNullLong helpers
    // -----------------------------------------------------------------------

    @Test
    void firstNonNull_explicitWins() {
        assertEquals(Integer.valueOf(4), PresetDefaults.firstNonNull(4, 2));
    }

    @Test
    void firstNonNull_nullExplicit_usesDefault() {
        assertEquals(Integer.valueOf(2), PresetDefaults.firstNonNull(null, 2));
    }

    @Test
    void firstNonNullLong_explicitWins() {
        assertEquals(3_000_000L, PresetDefaults.firstNonNullLong(3_000_000L, 500_000L));
    }

    @Test
    void firstNonNullLong_nullExplicit_usesDefault() {
        assertEquals(500_000L, PresetDefaults.firstNonNullLong(null, 500_000L));
    }

    // -----------------------------------------------------------------------
    // Constants sanity checks
    // -----------------------------------------------------------------------

    @Test
    void researchConstants_matchDesignPins() {
        assertEquals("supervisor", PresetDefaults.RESEARCH_TOPOLOGY,
                "RESEARCH_TOPOLOGY must be 'supervisor' (design-pinned)");
        assertEquals(5, PresetDefaults.RESEARCH_MAX_FANOUT_PER_ITERATION,
                "RESEARCH_MAX_FANOUT_PER_ITERATION must be 5 (design-pinned)");
        assertEquals("formal_report", PresetDefaults.RESEARCH_WRITER_STYLE,
                "RESEARCH_WRITER_STYLE must be 'formal_report' (design-pinned)");
        assertEquals(2, PresetDefaults.RESEARCH_MAX_CONCURRENT_TASKS,
                "RESEARCH_MAX_CONCURRENT_TASKS must be 2 (design-pinned, E6 per-agent admission)");
        assertEquals(14400, PresetDefaults.RESEARCH_TASK_TIMEOUT_SECONDS,
                "RESEARCH_TASK_TIMEOUT_SECONDS must be 14400 (4 h, E7 decision)");
    }

    @Test
    void validationConstants_defaultMaxConcurrentTasksIsPublic() {
        // ValidationConstants must expose DEFAULT_MAX_CONCURRENT_TASKS so PresetDefaults
        // can reference it. This confirms the constant is available.
        assertEquals(5, ValidationConstants.DEFAULT_MAX_CONCURRENT_TASKS,
                "DEFAULT_MAX_CONCURRENT_TASKS must be 5 (matches DB migration 0007)");
        assertEquals(500_000L, ValidationConstants.DEFAULT_BUDGET_MAX_PER_TASK,
                "DEFAULT_BUDGET_MAX_PER_TASK must be 500000 (matches DB migration 0007)");
        assertEquals(5_000_000L, ValidationConstants.DEFAULT_BUDGET_MAX_PER_HOUR,
                "DEFAULT_BUDGET_MAX_PER_HOUR must be 5000000 (matches DB migration 0007)");
    }

    // -----------------------------------------------------------------------
    // Helpers
    // -----------------------------------------------------------------------

    /**
     * Builds a minimal valid AgentConfigRequest with the given topology, preset, and supervisor.
     */
    private AgentConfigRequest minimalConfig(String topology, String preset, SupervisorConfigRequest supervisor) {
        return new AgentConfigRequest(
                "system prompt", "openai", "gpt-4o", 0.7, null, null, null, null, null,
                topology, preset, supervisor, null);
    }

    /**
     * Applies the preset with the given name to a minimal request (no explicit overrides).
     */
    private AgentCreateRequest applyPreset(String presetName) {
        AgentConfigRequest config = minimalConfig(null, presetName, null);
        AgentCreateRequest request = new AgentCreateRequest("Agent", config, null, null, null);
        return PresetDefaults.applyPreset(request);
    }

    /**
     * Creates an agent through the full {@link AgentService#createAgent} path (preset seeding +
     * {@code canonicalizeConfig} base-tool merge), captures the persisted {@code agent_config} JSON,
     * and returns the canonicalized {@code allowed_tools} list. Used to assert base-tool-merge
     * interactions (e.g. {@code plan_write} arriving via {@code BASE_PLATFORM_TOOLS}, not the preset).
     */
    private List<String> createAgentAndCapturePersistedTools(String presetName, String expectedTopology)
            throws Exception {
        return createAgentAndCapturePersistedTools(minimalConfig(expectedTopology, presetName, null));
    }

    /**
     * Builds a real ConfigValidationHelper for validation tests (using mocked repositories).
     */
    private ConfigValidationHelper buildHelper() {
        com.persistentagent.api.repository.ModelRepository modelRepo =
                mock(com.persistentagent.api.repository.ModelRepository.class);
        com.persistentagent.api.repository.ToolServerRepository toolServerRepo =
                mock(com.persistentagent.api.repository.ToolServerRepository.class);
        return new ConfigValidationHelper(modelRepo, toolServerRepo, agentRepository, new ObjectMapper(), false);
    }

    /**
     * Creates an agent through the full {@link AgentService#createAgent} path for an explicit
     * config (no helper-imposed preset/topology) and returns the canonicalized {@code allowed_tools}.
     * Used to pin the no-preset canonicalization behavior.
     */
    private List<String> createAgentAndCapturePersistedTools(AgentConfigRequest config) throws Exception {
        AgentCreateRequest request = new AgentCreateRequest("Agent", config, null, null, null);

        doNothing().when(configValidationHelper).validateAgentConfig(any());

        Timestamp now = Timestamp.from(Instant.now());
        Map<String, Object> repoResult = new LinkedHashMap<>();
        repoResult.put("created_at", now);
        repoResult.put("updated_at", now);

        ArgumentCaptor<String> jsonCaptor = ArgumentCaptor.forClass(String.class);
        when(agentRepository.insert(eq(TENANT_ID), anyString(), anyString(),
                jsonCaptor.capture(), anyInt(), anyLong(), anyLong()))
                .thenReturn(repoResult);

        agentService.createAgent(request);

        String persistedJson = jsonCaptor.getValue();
        AgentConfigRequest parsed = objectMapper.readValue(persistedJson, AgentConfigRequest.class);
        List<String> tools = parsed.allowedTools();
        assertNotNull(tools, "persisted allowed_tools must be non-null after canonicalization; got: " + persistedJson);
        return tools;
    }
}
