package com.persistentagent.api.model.request;

import com.fasterxml.jackson.annotation.JsonInclude;
import com.fasterxml.jackson.annotation.JsonProperty;
import jakarta.validation.constraints.DecimalMax;
import jakarta.validation.constraints.DecimalMin;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.Size;

import java.util.List;

public record AgentConfigRequest(
                @NotBlank(message = "system_prompt is required") @Size(max = 51200, message = "system_prompt must not exceed 50KB") @JsonProperty("system_prompt") String systemPrompt,

                @NotBlank(message = "provider is required") String provider,

                @NotBlank(message = "model is required") String model,

                @DecimalMin(value = "0.0", message = "temperature must be >= 0.0") @DecimalMax(value = "2.0", message = "temperature must be <= 2.0") Double temperature,

                @JsonProperty("allowed_tools") List<String> allowedTools,

                @Size(max = 50, message = "tool_servers must not exceed 50 entries") @JsonProperty("tool_servers") List<String> toolServers,

                SandboxConfigRequest sandbox,

                // When absent on the request, the persisted canonical JSON
                // omits the key entirely (per Track 5 design: no silent
                // defaults written to the row).
                @JsonInclude(JsonInclude.Include.NON_NULL) MemoryConfigRequest memory,

                // Track 7: Context Window Management. When absent on the request,
                // the persisted canonical JSON omits the key entirely (no silent
                // defaults written to the row — same pattern as memory above).
                @JsonInclude(JsonInclude.Include.NON_NULL)
                @JsonProperty("context_management")
                ContextManagementConfigRequest contextManagement,

                // Agent Modes — Supervisor Topology (S1). The internal graph-shape
                // selector. Enum {react, supervisor}; absent → treated as "react" at
                // read time. Immutable after agent creation (enforced in updateAgent).
                // No silent default is written into the persisted row.
                @JsonInclude(JsonInclude.Include.NON_NULL)
                @JsonProperty("topology")
                String topology,

                // The customer-facing preset selector. S1 only accepts/round-trips it
                // verbatim; the preset→defaults mapping and unknown-preset 400 are S2's job.
                @JsonInclude(JsonInclude.Include.NON_NULL)
                @JsonProperty("preset")
                String preset,

                // Deep Research tuning sub-object. Optional; partial payloads accepted.
                // When absent, the persisted JSON omits the key entirely.
                @JsonInclude(JsonInclude.Include.NON_NULL)
                @JsonProperty("supervisor")
                SupervisorConfigRequest supervisor) {
}
