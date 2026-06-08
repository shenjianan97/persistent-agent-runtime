package com.persistentagent.api.service;

import com.persistentagent.api.config.ValidationConstants;
import com.persistentagent.api.model.request.AgentConfigRequest;
import com.persistentagent.api.model.request.AgentCreateRequest;
import com.persistentagent.api.model.request.SupervisorConfigRequest;

import java.util.ArrayList;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Set;

/**
 * Platform-owned preset bundles for the Agent Modes / Supervisor Topology track (S2).
 *
 * <p>A <strong>preset</strong> is a named default bundle applied <em>at agent creation only</em>
 * (never on PUT). It seeds: {@code topology}, the tool allowlist ({@code allowed_tools}),
 * the agent-level concurrency/budget columns, the per-task-default timeout (as
 * {@code agent_config.task_timeout_seconds}), and the {@code supervisor} sub-object (for the
 * {@code research} preset). Explicit request fields always override preset defaults — a preset
 * only fills a field the request left absent.
 *
 * <p><strong>Override rule:</strong> for every field a preset seeds, the request value wins
 * if present; the preset fills only absent ({@code null}/unset) fields. This is S2's one
 * deliberate divergence from the "no silent defaults" rule — it is bounded to
 * {@link AgentService#createAgent} only, never {@code PUT}.
 *
 * <p><strong>Topology-vs-preset contradiction:</strong> if a request names a preset AND explicitly
 * sets a {@code topology} value that differs from the preset's topology, the request is rejected
 * with 400. Rationale: picking {@code preset=research} and {@code topology=react} simultaneously
 * is an ambiguous, contradictory intent — "clearer to reject" (spec §Shared Contract). Documented
 * in {@link #validatePresetTopologyConsistency}.
 *
 * <p><strong>Design reference:</strong> {@code docs/design-docs/agent-modes/design.md} — section
 * "Presets" (the preset table); plan §A4.1 S2 row; plan §A6.4 (E6: {@code max_concurrent_tasks=2}
 * is per-agent admission, NOT a cross-tenant guard — the binding limit is the per-worker-process
 * semaphore in {@code config.max_concurrent_tasks}).
 *
 * <p><strong>Known presets:</strong>
 * <ul>
 *   <li>{@code chat} — ReAct, light, planning off, customer-defined tools, small budget.
 *   <li>{@code coding} — ReAct, coding + sandbox tools, {@code dispatch_subagent} in the
 *       allowlist, larger budget. ({@code dispatch_subagent} is a Track-7-precedent: seeded
 *       ahead of S4 runtime delivery.) {@code plan_write} is NOT seeded here — it is a base
 *       platform tool merged into every agent by {@code canonicalizeConfig} (2026-06-06
 *       product decision, PR #115; see {@link ValidationConstants#BASE_PLATFORM_TOOLS}).
 *   <li>{@code investigation} — ReAct, broad allowlist, {@code dispatch_subagent}.
 *       ({@code plan_write} likewise arrives via the base-tool merge, not the preset.)
 *   <li>{@code research} — Supervisor, web tools, fan-out 5, formal-report writer,
 *       {@code max_concurrent_tasks=2} (per-agent; see E6 NOTE), {@code task_timeout_seconds=14400}.
 *   <li>{@code workflow_runner} — Declared (name reserved) but not actionable. Phase 3 target.
 *       Returns {@code react} topology as a placeholder. See Javadoc on the constant.
 * </ul>
 */
public final class PresetDefaults {

    private PresetDefaults() {
    }

    // -----------------------------------------------------------------------
    // Known preset names (load-bearing set — S2's source of truth)
    // -----------------------------------------------------------------------

    /** All preset names the platform recognises. An absent preset is valid (no seeding). */
    public static final java.util.Set<String> KNOWN_PRESETS = java.util.Set.of(
            "chat", "coding", "investigation", "research", "workflow_runner");

    // -----------------------------------------------------------------------
    // chat preset constants
    // -----------------------------------------------------------------------

    /**
     * {@code chat} seeds a small per-task budget: 250 000 microdollars (~$0.25).
     * Reasoning: chat agents are low-cost, short-turn interactions; a tight budget
     * matches the use case while the platform default (500 000 µ$) is already conservative.
     * Tools are customer-defined — {@code chat} does not seed an allowlist beyond the
     * base platform tools that {@code canonicalizeConfig} always applies.
     */
    static final long CHAT_BUDGET_MAX_PER_TASK = 250_000L;

    /** {@code chat} uses the platform default budget-per-hour. */
    static final long CHAT_BUDGET_MAX_PER_HOUR = ValidationConstants.DEFAULT_BUDGET_MAX_PER_HOUR;

    /** {@code chat} keeps the platform default concurrency (5). */
    static final int CHAT_MAX_CONCURRENT_TASKS = ValidationConstants.DEFAULT_MAX_CONCURRENT_TASKS;

    // -----------------------------------------------------------------------
    // coding preset constants
    // -----------------------------------------------------------------------

    /**
     * {@code coding} seeds a larger per-task budget: 2 000 000 microdollars (~$2).
     * Reasoning: coding agents iterate over a codebase, compile/test, and may fan out
     * via {@code dispatch_subagent}; the higher ceiling accommodates multi-step runs
     * without hitting the platform default at the first iteration loop. The per-hour
     * budget is kept at the platform default to remain reasonable under concurrent tasks.
     */
    static final long CODING_BUDGET_MAX_PER_TASK = 2_000_000L;

    /** {@code coding} uses the platform default budget-per-hour. */
    static final long CODING_BUDGET_MAX_PER_HOUR = ValidationConstants.DEFAULT_BUDGET_MAX_PER_HOUR;

    /** {@code coding} uses the platform default concurrency (5). */
    static final int CODING_MAX_CONCURRENT_TASKS = ValidationConstants.DEFAULT_MAX_CONCURRENT_TASKS;

    /**
     * Tools seeded by the {@code coding} preset in addition to the base platform tools
     * that {@code canonicalizeConfig} always adds.
     *
     * <ul>
     *   <li>{@code dispatch_subagent} — Track-7 precedent: seeded ahead of S4 runtime.
     *       Allowed in the allowlist before the runtime wires it.</li>
     *   <li>Sandbox tools — coding agents need code execution.</li>
     * </ul>
     *
     * <p><strong>{@code plan_write} is intentionally NOT seeded here.</strong> By product
     * decision (2026-06-06, PR #115 — same decision recorded on
     * {@link ValidationConstants#BASE_PLATFORM_TOOLS}), {@code plan_write} is a <em>base
     * platform tool</em>: {@code canonicalizeConfig} merges {@code BASE_PLATFORM_TOOLS} into
     * <em>every</em> agent's {@code allowed_tools}, so a {@code coding} agent already has
     * {@code plan_write} via that merge. The earlier plan/§A11 note saying this preset seeds
     * {@code plan_write} is outdated — seeding it again here would be redundant. (Coverage:
     * {@code PresetDefaultsTest#codingPreset_planWritePresentViaBaseTools}.)
     *
     * <p>Note: base platform tools ({@code web_search}, {@code read_url},
     * {@code create_text_artifact}, {@code request_human_input}, {@code plan_write}) are
     * added by {@code canonicalizeConfig} unconditionally; we list only the
     * <em>additional</em> tools here. {@code sandbox_read_file}, {@code sandbox_write_file},
     * {@code export_sandbox_file} are from {@link ValidationConstants#SANDBOX_TOOLS} but
     * seeded separately here because the preset sets the tool allowlist directly rather
     * than relying on the sandbox.enabled canonicalization path (the customer may not set
     * sandbox.enabled explicitly; the preset ensures the tools are declared).
     */
    static final List<String> CODING_EXTRA_TOOLS = List.of(
            "dispatch_subagent",
            // sandbox tools (subset of SANDBOX_TOOLS — seeded by this preset so a coding
            // agent gets code execution primitives without requiring sandbox.enabled=true
            // in the config sub-object)
            "sandbox_exec", "sandbox_read_file", "sandbox_write_file", "export_sandbox_file");

    // -----------------------------------------------------------------------
    // investigation preset constants
    // -----------------------------------------------------------------------

    /**
     * {@code investigation} seeds a moderate per-task budget: 1 000 000 microdollars (~$1).
     * Reasoning: investigation agents do multi-step research with a broad tool allowlist
     * and may use {@code dispatch_subagent}; more headroom than {@code chat} but less
     * than {@code coding} (investigations are typically read-only, not compile/test loops).
     */
    static final long INVESTIGATION_BUDGET_MAX_PER_TASK = 1_000_000L;

    /** {@code investigation} uses the platform default budget-per-hour. */
    static final long INVESTIGATION_BUDGET_MAX_PER_HOUR = ValidationConstants.DEFAULT_BUDGET_MAX_PER_HOUR;

    /** {@code investigation} uses the platform default concurrency (5). */
    static final int INVESTIGATION_MAX_CONCURRENT_TASKS = ValidationConstants.DEFAULT_MAX_CONCURRENT_TASKS;

    /**
     * Additional tools seeded by the {@code investigation} preset beyond the base platform tools.
     * {@code dispatch_subagent} is seeded ahead of S4 runtime per Track-7 precedent.
     *
     * <p><strong>{@code plan_write} is intentionally NOT seeded here</strong> — it is a base
     * platform tool merged into every agent by {@code canonicalizeConfig} (2026-06-06 product
     * decision, PR #115; see {@link ValidationConstants#BASE_PLATFORM_TOOLS}). The earlier
     * plan/§A11 note saying this preset seeds {@code plan_write} is outdated. (Coverage:
     * {@code PresetDefaultsTest#investigationPreset_planWritePresentViaBaseTools}.)
     */
    static final List<String> INVESTIGATION_EXTRA_TOOLS = List.of("dispatch_subagent");

    // -----------------------------------------------------------------------
    // research preset constants (design-pinned — exact values, do not change without spec update)
    // -----------------------------------------------------------------------
    // (PRESET_INJECTED_TOOLS is declared after RESEARCH_TOOLS, once all the EXTRA-tool
    //  lists it unions over are defined — see below.)

    /**
     * {@code research} sets {@code topology=supervisor}: the only platform preset that
     * produces a Supervisor-topology agent.
     */
    static final String RESEARCH_TOPOLOGY = "supervisor";

    /**
     * {@code research} seeds {@code supervisor.max_fanout_per_iteration=5}: fan out up to
     * 5 sub-agents per Supervisor iteration. Design-pinned value.
     */
    static final int RESEARCH_MAX_FANOUT_PER_ITERATION = 5;

    /**
     * {@code research} seeds {@code supervisor.writer_style=formal_report}. Design-pinned value.
     */
    static final String RESEARCH_WRITER_STYLE = "formal_report";

    /**
     * {@code research} seeds {@code agents.max_concurrent_tasks=2}.
     *
     * <p><strong>E6 NOTE (plan §A6.4, §A11-E6):</strong> this is the per-AGENT admission
     * limit stored in the {@code agents} DB column — NOT a cross-tenant worker-slot guard.
     * A small number of multi-minute Supervisor fan-outs from <em>different</em> agents can
     * still saturate the per-worker-process {@code asyncio.Semaphore}
     * ({@code config.max_concurrent_tasks}). The real mitigation is worker-pool sizing and
     * isolation (ops — plan §A6.4 / §A11-E6). Keep this value at 2 for per-agent admission
     * (correct); do not represent it as the cross-tenant guard.
     */
    static final int RESEARCH_MAX_CONCURRENT_TASKS = 2;

    /**
     * {@code research} seeds {@code agent_config.task_timeout_seconds=14400} (4 hours).
     *
     * <p><strong>E7 NOTE (plan §A6.5, §A11-E7):</strong> a Deep Research run is <em>one task</em>.
     * The reaper dead-letters any task where {@code timeout_reference_at + task_timeout_seconds < NOW()}
     * ({@code core/reaper.py:98}), and {@code timeout_reference_at} is set once at creation —
     * independent of the healthy lease heartbeat — so a wide multi-iteration fan-out exceeding
     * the platform default of 3600 s would be dead-lettered mid-run.
     *
     * <p>Size arithmetic (worst-case wall-clock): {@code max_iterations(default≈3) ×
     * max_fanout_per_iteration(5) × per-sub-agent-ceiling(~180 s)} ≈ 2700 s (45 min).
     * A more cautious bound adds Writer, Scope, and iteration overhead: ~4× the
     * per-sub-agent budget → 3 × 5 × 180 × 1.5 ≈ 4050 s. We round up to
     * <strong>14 400 s (4 h)</strong> to accommodate slow web fetches, model latency spikes,
     * and multiple-iteration runs with large fan-out. This is tunable; the 4 h default is
     * documented as a starting point, not a hard limit.
     *
     * <p>This field is stored as {@code agent_config.task_timeout_seconds} (JSONB) — NOT
     * inside the {@code supervisor} sub-object and NOT in a separate DB column.
     * The {@code TaskService} submission-time fallback reads it from the agent config when
     * the task submission omits {@code task_timeout_seconds}.
     */
    static final int RESEARCH_TASK_TIMEOUT_SECONDS = 14_400;

    /**
     * Tool allowlist seeded by the {@code research} preset. Only web tools: the Supervisor
     * uses subagents for research, and subagents need web access.
     *
     * <p>Names verified against the worker tool registry ({@code tools/definitions.py}):
     * {@code web_search} at line 165 and {@code read_url} at line 171.
     */
    static final List<String> RESEARCH_TOOLS = List.of("web_search", "read_url");

    // -----------------------------------------------------------------------
    // Preset-injected tool names (carry-through allowlist for canonicalizeConfig)
    // -----------------------------------------------------------------------

    /**
     * The union of every tool name a preset can inject into {@code allowed_tools}.
     *
     * <p>{@code AgentService.canonicalizeConfig} rebuilds {@code allowed_tools}
     * deterministically from config flags (BASE + SANDBOX-if-enabled + DEV-if-enabled).
     * Pre-S2 that derivation <em>dropped</em> any other name in {@code allowed_tools}
     * (validation is a closed allowlist, so only known platform tools ever reach it, and
     * tool-server/BYOT tools travel through the separate {@code tool_servers} field — they
     * never appear here). To keep the <strong>no-preset path byte-for-byte unchanged</strong>,
     * canonicalisation carries through <em>only</em> the names in this set — the tools a
     * preset injects post-validation (e.g. {@code dispatch_subagent}, which is not yet in
     * {@code ValidationConstants.ALLOWED_TOOLS}; Track-7 precedent). Tools already produced
     * by the base/sandbox derivation (e.g. the research preset's {@code web_search}/{@code read_url},
     * the coding preset's sandbox tools) are not listed here because the derivation already
     * emits them.
     *
     * <p>When a preset starts injecting a brand-new tool name, add it here (and to that
     * preset's {@code *_EXTRA_TOOLS}); otherwise canonicalisation will silently drop it.
     */
    static final Set<String> PRESET_INJECTED_TOOLS;
    static {
        Set<String> injected = new LinkedHashSet<>();
        injected.addAll(CODING_EXTRA_TOOLS);
        injected.addAll(INVESTIGATION_EXTRA_TOOLS);
        injected.addAll(RESEARCH_TOOLS);
        PRESET_INJECTED_TOOLS = Set.copyOf(injected);
    }

    // -----------------------------------------------------------------------
    // workflow_runner preset — declared, not wired (Phase 3)
    // -----------------------------------------------------------------------

    /**
     * {@code workflow_runner} is a reserved preset name.
     *
     * <p><strong>Decision (S2, 2026-06-08):</strong> declare the name as recognised
     * (so validation accepts it) but seed it as a no-op {@code react} placeholder.
     * No {@code execute_workflow} tool, no {@code workflow_id} submission plumbing,
     * and no step-list machinery are wired. This is a Phase 3 target (plan §A9
     * "Do NOT build"; design *Workflow as a resource*). The placeholder topology keeps
     * the validator from returning 400 for a future customer who specifies this name;
     * the operator note below surfaces it clearly.
     *
     * <p>Note: if a customer creates an agent with {@code preset=workflow_runner} today,
     * it behaves as a plain {@code react} agent with no preset-specific tools or config
     * — a safe, actionable result. The Console (S10) should surface a "workflow support
     * coming soon" indicator for this preset; that is S10's concern.
     */
    // No special constants needed — workflow_runner seeds nothing actionable.
    // KNOWN_PRESETS includes it; applyPreset treats it as a no-op topology=react seed.

    // -----------------------------------------------------------------------
    // Public API
    // -----------------------------------------------------------------------

    /**
     * Returns the {@code topology} value this preset seeds, or {@code null} if the preset
     * does not seed a topology (i.e. leaves the field absent). Used by
     * {@link #validatePresetTopologyConsistency} to detect contradictions.
     *
     * <p>All current presets seed a topology, so this never returns {@code null} for a
     * known preset. The return value is load-bearing for the contradiction check.
     */
    static String presetTopology(String preset) {
        if (preset == null) {
            return null;
        }
        return switch (preset) {
            case "research" -> RESEARCH_TOPOLOGY; // "supervisor"
            case "chat", "coding", "investigation", "workflow_runner" -> "react";
            default -> null; // unknown preset — handled by unknown-preset 400 elsewhere
        };
    }

    /**
     * Validates that a request naming a preset does not simultaneously set an explicit
     * {@code topology} that contradicts the preset's topology.
     *
     * <p><strong>Decision (S2):</strong> a request with {@code preset=research} and
     * {@code topology=react} is rejected 400 with a clear message. Rationale: the two fields
     * express contradictory shape choices; the "explicit wins" override rule applies per-field
     * but not when the preset's whole identity is its topology. Rejecting is clearer than
     * silently discarding either value.
     *
     * <p>If the request omits topology (null), the preset's topology is used — no contradiction.
     * If the request names a topology that matches the preset's topology, also no contradiction.
     *
     * @param preset   the preset name from the request (may be null — no-op if null)
     * @param topology the explicit topology from the request (may be null)
     * @throws com.persistentagent.api.exception.ValidationException if a contradiction is detected
     */
    public static void validatePresetTopologyConsistency(String preset, String topology) {
        if (preset == null || topology == null) {
            return; // No contradiction possible when either is absent.
        }
        String seededTopology = presetTopology(preset);
        if (seededTopology != null && !seededTopology.equals(topology)) {
            throw new com.persistentagent.api.exception.ValidationException(
                    "preset '" + preset + "' sets topology='" + seededTopology + "' but the request"
                    + " explicitly sets topology='" + topology + "' — remove one or make them consistent");
        }
    }

    /**
     * Applies preset defaults to an {@link AgentCreateRequest} at creation time.
     *
     * <p>This is the sole entry point for preset seeding. It is called in
     * {@link AgentService#createAgent} <em>after</em> S1 validation (topology/supervisor bounds)
     * and <em>before</em> {@code canonicalizeConfig} serialises. The resulting values are
     * real, persisted values — preset seeding is the deliberate exception to the "no silent
     * defaults" canonicalisation rule.
     *
     * <p>Override rule applied here: for every field a preset can seed, the request value wins
     * if non-null. The preset fills only absent fields.
     *
     * <p>Seeding happens in two layers:
     * <ol>
     *   <li><strong>agent_config fields</strong> (topology, tool allowlist extras,
     *       supervisor sub-object, task_timeout_seconds): returned as a new merged
     *       {@link AgentConfigRequest}.</li>
     *   <li><strong>agents row columns</strong> (max_concurrent_tasks, budget_max_per_task,
     *       budget_max_per_hour): returned as a new merged {@link AgentCreateRequest} with
     *       preset values inserted into the fallback chain
     *       (request value → preset value → {@code DEFAULT_*}).</li>
     * </ol>
     *
     * @param request the original create request (unchanged)
     * @return a new request with preset defaults filled in; identical to input if no preset
     */
    public static AgentCreateRequest applyPreset(AgentCreateRequest request) {
        String preset = request.agentConfig() != null ? request.agentConfig().preset() : null;
        if (preset == null) {
            return request; // No preset — no seeding. Pre-S2 behavior preserved.
        }

        AgentConfigRequest cfg = request.agentConfig();

        return switch (preset) {
            case "chat" -> applyChatPreset(request, cfg);
            case "coding" -> applyCodingPreset(request, cfg);
            case "investigation" -> applyInvestigationPreset(request, cfg);
            case "research" -> applyResearchPreset(request, cfg);
            case "workflow_runner" -> applyWorkflowRunnerPreset(request, cfg);
            default ->
                    // Unknown preset — this path is unreachable if validatePreset() was called
                    // first (it rejects unknown presets with 400). Defensive fallthrough: no seeding.
                    request;
        };
    }

    // -----------------------------------------------------------------------
    // Per-preset apply methods
    // -----------------------------------------------------------------------

    private static AgentCreateRequest applyChatPreset(AgentCreateRequest request, AgentConfigRequest cfg) {
        // chat: topology=react, light budget, tools = customer-defined (no preset seeding).
        // canonicalizeConfig always adds BASE_PLATFORM_TOOLS; we don't add extras here.
        AgentConfigRequest mergedConfig = mergeTopology(cfg, "react");

        int maxConcurrentTasks = firstNonNull(request.maxConcurrentTasks(), CHAT_MAX_CONCURRENT_TASKS);
        long budgetMaxPerTask = firstNonNullLong(request.budgetMaxPerTask(), CHAT_BUDGET_MAX_PER_TASK);
        long budgetMaxPerHour = firstNonNullLong(request.budgetMaxPerHour(), CHAT_BUDGET_MAX_PER_HOUR);

        return new AgentCreateRequest(
                request.displayName(), mergedConfig, maxConcurrentTasks, budgetMaxPerTask, budgetMaxPerHour);
    }

    private static AgentCreateRequest applyCodingPreset(AgentCreateRequest request, AgentConfigRequest cfg) {
        // coding: topology=react, dispatch_subagent + sandbox tools in allowlist, larger
        // per-task budget. (plan_write is NOT seeded here — it arrives via the
        // BASE_PLATFORM_TOOLS merge in canonicalizeConfig; see CODING_EXTRA_TOOLS Javadoc.)
        AgentConfigRequest mergedConfig = mergeTopologyAndExtraTools(cfg, "react", CODING_EXTRA_TOOLS);

        int maxConcurrentTasks = firstNonNull(request.maxConcurrentTasks(), CODING_MAX_CONCURRENT_TASKS);
        long budgetMaxPerTask = firstNonNullLong(request.budgetMaxPerTask(), CODING_BUDGET_MAX_PER_TASK);
        long budgetMaxPerHour = firstNonNullLong(request.budgetMaxPerHour(), CODING_BUDGET_MAX_PER_HOUR);

        return new AgentCreateRequest(
                request.displayName(), mergedConfig, maxConcurrentTasks, budgetMaxPerTask, budgetMaxPerHour);
    }

    private static AgentCreateRequest applyInvestigationPreset(AgentCreateRequest request, AgentConfigRequest cfg) {
        // investigation: topology=react, broad allowlist (dispatch_subagent + web tools).
        // Base tools (web_search, read_url, plan_write, etc.) are already in BASE_PLATFORM_TOOLS
        // and arrive via the canonicalizeConfig merge; only extra tools not covered by the base
        // list need seeding here (just dispatch_subagent).
        AgentConfigRequest mergedConfig = mergeTopologyAndExtraTools(cfg, "react", INVESTIGATION_EXTRA_TOOLS);

        int maxConcurrentTasks = firstNonNull(request.maxConcurrentTasks(), INVESTIGATION_MAX_CONCURRENT_TASKS);
        long budgetMaxPerTask = firstNonNullLong(request.budgetMaxPerTask(), INVESTIGATION_BUDGET_MAX_PER_TASK);
        long budgetMaxPerHour = firstNonNullLong(request.budgetMaxPerHour(), INVESTIGATION_BUDGET_MAX_PER_HOUR);

        return new AgentCreateRequest(
                request.displayName(), mergedConfig, maxConcurrentTasks, budgetMaxPerTask, budgetMaxPerHour);
    }

    private static AgentCreateRequest applyResearchPreset(AgentCreateRequest request, AgentConfigRequest cfg) {
        // research: topology=supervisor, web tools, fan-out=5, formal_report writer,
        // max_concurrent_tasks=2, task_timeout_seconds=14400.
        // Supervisor sub-object: seed only absent sub-fields (explicit wins per sub-field).
        SupervisorConfigRequest existingSupervisor = cfg.supervisor();
        SupervisorConfigRequest seededSupervisor = new SupervisorConfigRequest(
                // max_fanout_per_iteration: explicit wins; seed 5 if absent.
                firstNonNull(existingSupervisor != null ? existingSupervisor.maxFanoutPerIteration() : null,
                        RESEARCH_MAX_FANOUT_PER_ITERATION),
                // max_iterations: not seeded by this preset; preserve existing or leave absent.
                existingSupervisor != null ? existingSupervisor.maxIterations() : null,
                // source_allowlist: not seeded; preserve existing.
                existingSupervisor != null ? existingSupervisor.sourceAllowlist() : null,
                // writer_style: seed formal_report if absent.
                existingSupervisor != null && existingSupervisor.writerStyle() != null
                        ? existingSupervisor.writerStyle() : RESEARCH_WRITER_STYLE,
                // scope_clarification_enabled: not seeded; preserve existing.
                existingSupervisor != null ? existingSupervisor.scopeClarificationEnabled() : null);

        // task_timeout_seconds: seeded into agent_config JSONB (not a DB column today).
        // Explicit value in the request's agentConfig wins; otherwise use the preset default.
        // This is read by TaskService at submission time as the agent-level default.
        Integer taskTimeoutSeconds = firstNonNull(cfg.taskTimeoutSeconds(), RESEARCH_TASK_TIMEOUT_SECONDS);

        // Tool allowlist: preset seeds web tools; canonicalizeConfig adds BASE_PLATFORM_TOOLS.
        // These extra tools are merged into what the request provides (or the base list).
        List<String> extraTools = RESEARCH_TOOLS; // ["web_search", "read_url"] — already in base

        AgentConfigRequest mergedConfig = new AgentConfigRequest(
                cfg.systemPrompt(),
                cfg.provider(),
                cfg.model(),
                cfg.temperature(),
                mergeExtraTools(cfg.allowedTools(), extraTools),
                cfg.toolServers(),
                cfg.sandbox(),
                cfg.memory(),
                cfg.contextManagement(),
                // topology: preset seeds "supervisor"; explicit contradictions already rejected by
                // validatePresetTopologyConsistency in ConfigValidationHelper.
                firstNonNull(cfg.topology(), RESEARCH_TOPOLOGY),
                cfg.preset(),
                seededSupervisor,
                taskTimeoutSeconds);

        int maxConcurrentTasks = firstNonNull(request.maxConcurrentTasks(), RESEARCH_MAX_CONCURRENT_TASKS);
        // research uses platform-default budgets; they are NOT pinned by the preset design.
        long budgetMaxPerTask = firstNonNullLong(request.budgetMaxPerTask(), ValidationConstants.DEFAULT_BUDGET_MAX_PER_TASK);
        long budgetMaxPerHour = firstNonNullLong(request.budgetMaxPerHour(), ValidationConstants.DEFAULT_BUDGET_MAX_PER_HOUR);

        return new AgentCreateRequest(
                request.displayName(), mergedConfig, maxConcurrentTasks, budgetMaxPerTask, budgetMaxPerHour);
    }

    private static AgentCreateRequest applyWorkflowRunnerPreset(AgentCreateRequest request, AgentConfigRequest cfg) {
        // workflow_runner: Phase 3 — declared but not actionable. Seeds topology=react as a
        // placeholder so the agent is usable. See class Javadoc for the decision rationale.
        AgentConfigRequest mergedConfig = mergeTopology(cfg, "react");

        // No budget or concurrency overrides; use request values or platform defaults.
        return new AgentCreateRequest(
                request.displayName(), mergedConfig,
                request.maxConcurrentTasks(), request.budgetMaxPerTask(), request.budgetMaxPerHour());
    }

    // -----------------------------------------------------------------------
    // Merge helpers
    // -----------------------------------------------------------------------

    /**
     * Returns a new {@link AgentConfigRequest} with topology set to {@code presetTopology}
     * if the request did not supply an explicit topology. The request value wins if present.
     */
    private static AgentConfigRequest mergeTopology(AgentConfigRequest cfg, String presetTopology) {
        return new AgentConfigRequest(
                cfg.systemPrompt(), cfg.provider(), cfg.model(), cfg.temperature(),
                cfg.allowedTools(), cfg.toolServers(), cfg.sandbox(),
                cfg.memory(), cfg.contextManagement(),
                firstNonNull(cfg.topology(), presetTopology),
                cfg.preset(), cfg.supervisor(), cfg.taskTimeoutSeconds());
    }

    /**
     * Returns a new {@link AgentConfigRequest} with topology and extra tool names merged in.
     * The extra tools are appended to the request's existing {@code allowedTools} list
     * (deduplication happens in {@code canonicalizeConfig}; we just ensure the names are present).
     */
    private static AgentConfigRequest mergeTopologyAndExtraTools(
            AgentConfigRequest cfg, String presetTopology, List<String> extraTools) {
        return new AgentConfigRequest(
                cfg.systemPrompt(), cfg.provider(), cfg.model(), cfg.temperature(),
                mergeExtraTools(cfg.allowedTools(), extraTools),
                cfg.toolServers(), cfg.sandbox(),
                cfg.memory(), cfg.contextManagement(),
                firstNonNull(cfg.topology(), presetTopology),
                cfg.preset(), cfg.supervisor(), cfg.taskTimeoutSeconds());
    }

    /**
     * Merges extra tools into the provided tools list, returning a new combined list.
     * Avoids duplicates: does not add a tool if it is already present.
     * If the existing list is null, returns a new list containing only the extra tools
     * (canonicalizeConfig will add BASE_PLATFORM_TOOLS on top).
     */
    static List<String> mergeExtraTools(List<String> existingTools, List<String> extraTools) {
        if (extraTools == null || extraTools.isEmpty()) {
            return existingTools;
        }
        List<String> merged = existingTools != null ? new ArrayList<>(existingTools) : new ArrayList<>();
        for (String tool : extraTools) {
            if (!merged.contains(tool)) {
                merged.add(tool);
            }
        }
        return merged;
    }

    // -----------------------------------------------------------------------
    // Null-coalescing helpers (explicit wins — preset fills only absent fields)
    // -----------------------------------------------------------------------

    static <T> T firstNonNull(T explicit, T presetDefault) {
        return explicit != null ? explicit : presetDefault;
    }

    // firstNonNullLong is kept (not collapsed into the generic firstNonNull<T>) because the
    // primitive-long preset default avoids autoboxing the constant on every call.
    static long firstNonNullLong(Long explicit, long presetDefault) {
        return explicit != null ? explicit : presetDefault;
    }
}
