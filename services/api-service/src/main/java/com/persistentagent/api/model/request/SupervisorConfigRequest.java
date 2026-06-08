package com.persistentagent.api.model.request;

import com.fasterxml.jackson.annotation.JsonProperty;

import java.util.List;

/**
 * Nested Supervisor tuning configuration carried inside {@link AgentConfigRequest}.
 *
 * <p>Five optional tuning fields for the Deep Research / Supervisor topology.
 * All fields are nullable so partial payloads are accepted — absence is always
 * valid at the API surface. Bounds and enum checks live in
 * {@code ConfigValidationHelper.validateSupervisorConfig}, not here, to keep
 * error messages consistent with the existing helper style.
 *
 * <p>Canonicalisation preserves all five fields verbatim when the sub-object is
 * present, and omits the sub-object entirely when absent. No platform defaults
 * are written into the persisted config row — defaults apply at read time in the
 * worker (S8) per the Agent Modes design.
 *
 * <p>Because Spring Boot's Jackson is configured with
 * {@code FAIL_ON_UNKNOWN_PROPERTIES = true}, a client sending an unrecognised
 * key inside this sub-object receives a 400 — no manual guard needed.
 *
 * <p>See {@code docs/design-docs/agent-modes/design.md} — "Topology 2: Supervisor"
 * → "What customers configure", and the Supervisor Topology track plan §A4.
 */
public record SupervisorConfigRequest(

        /**
         * Maximum number of sub-agents to fan out in a single Supervisor iteration.
         * Bounds: [1, 20]. When absent, the worker applies its v1 default.
         */
        @JsonProperty("max_fanout_per_iteration")
        Integer maxFanoutPerIteration,

        /**
         * Maximum number of Supervisor iterations (rounds of fan-out) for a single
         * research run. Bounds: [1, 10]. When absent, the worker applies its v1 default.
         */
        @JsonProperty("max_iterations")
        Integer maxIterations,

        /**
         * Optional allowlist of source names (web tools, document stores) that
         * sub-agents may use. At most 50 entries. Entry contents are not validated —
         * customers may name tools or stores not yet wired.
         */
        @JsonProperty("source_allowlist")
        List<String> sourceAllowlist,

        /**
         * Output format for the Writer node. One of {@code "formal_report"} or
         * {@code "annotated_bullets"}. When absent, the worker uses its v1 default.
         */
        @JsonProperty("writer_style")
        String writerStyle,

        /**
         * Whether the Scope node is allowed to ask the user for clarification before
         * starting the research run. Set to {@code false} for headless / automated
         * operation where no user is present to answer.
         */
        @JsonProperty("scope_clarification_enabled")
        Boolean scopeClarificationEnabled) {
}
