package com.persistentagent.api.model.request;

import com.fasterxml.jackson.annotation.JsonProperty;

import java.util.List;

/**
 * Nested context-management configuration carried inside {@link AgentConfigRequest}.
 *
 * <p>Three tuning fields only — no {@code enabled} toggle. Track 7 (Context Window Management)
 * is always-on platform infrastructure; per-agent opt-out is intentionally excluded.
 *
 * <p>All fields are nullable so partial payloads are accepted. Absence is always valid at
 * the API surface — platform defaults apply at read time (worker / Task 3), not at write
 * time. Canonicalisation preserves the sub-object verbatim when present and omits it
 * entirely when absent.
 *
 * <p>Because Spring Boot's Jackson is configured with {@code FAIL_ON_UNKNOWN_PROPERTIES = true},
 * a client sending {@code "enabled": true} inside this sub-object will receive a 400
 * "Unrecognized field 'enabled'" — no manual guard needed; the default Jackson behaviour
 * enforces it.
 *
 * <p>See {@code docs/design-docs/phase-2/track-7-context-window-management.md} — "Agent
 * config extension" and "Validation and consistency rules".
 */
public record ContextManagementConfigRequest(

        /**
         * Optional summarizer model for Tier 3 LLM summarization.
         * When absent, the worker falls back to the platform default
         * (defined in Task 3 worker constants).
         */
        @JsonProperty("summarizer_model")
        String summarizerModel,

        /**
         * Optional provider for {@code summarizer_model}. When absent, the API
         * preserves legacy behaviour and treats the agent's primary provider as
         * the summarizer provider.
         */
        @JsonProperty("summarizer_provider")
        String summarizerProvider,

        /**
         * Optional list of tool names whose results must never be masked by Tier 1
         * observation-clearing. Additive to the platform's built-in exclude list
         * ({@code memory_note}, {@code save_memory}, {@code request_human_input},
         * {@code memory_search}, {@code task_history_get}). Max 50 entries.
         */
        @JsonProperty("exclude_tools")
        List<String> excludeTools,

        /**
         * Optional flag enabling a one-shot memory-flush agentic turn before the first
         * Tier 3 summarization fires. No-op when {@code agent.memory.enabled = false};
         * runtime gating is the worker's responsibility (Task 9).
         */
        @JsonProperty("pre_tier3_memory_flush")
        Boolean preTier3MemoryFlush,

        /**
         * Optional kill-switch for Tier 0 ingestion offload of oversized tool results
         * and oversized tool-call args (Track 7 Follow-up, Task 4). Default {@code true}
         * — when {@code null} the worker applies {@code true}. Set {@code false} to keep
         * all tool-result content and tool-call args inline regardless of size (matches
         * pre-Follow-up behaviour). Not Console-editable in v1; operators needing to
         * disable per-agent use the agent-update API directly.
         */
        @JsonProperty("offload_tool_results")
        Boolean offloadToolResults) {
}
