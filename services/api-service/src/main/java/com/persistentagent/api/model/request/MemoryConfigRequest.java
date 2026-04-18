package com.persistentagent.api.model.request;

import com.fasterxml.jackson.annotation.JsonProperty;

/**
 * Nested memory configuration carried inside {@link AgentConfigRequest}.
 *
 * <p>All fields are nullable so partial payloads are accepted. Absence is always
 * valid at the API surface — platform defaults apply at read time (worker /
 * validator), not at write time. Canonicalisation preserves the sub-object
 * verbatim when present and omits it entirely when absent.
 *
 * <p>See {@code docs/design-docs/phase-2/track-5-memory.md} — "Agent table
 * extension" and "Validation and Consistency Rules".
 */
public record MemoryConfigRequest(
        Boolean enabled,

        @JsonProperty("summarizer_model")
        String summarizerModel,

        @JsonProperty("max_entries")
        Integer maxEntries) {
}
