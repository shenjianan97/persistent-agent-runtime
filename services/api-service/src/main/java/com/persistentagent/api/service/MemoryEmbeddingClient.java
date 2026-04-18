package com.persistentagent.api.service;

/**
 * Thin Java-side embedding provider client used by the memory search endpoint
 * (Task 3 of Phase 2 Track 5).
 *
 * <p>v1 constraints:
 * <ul>
 *     <li>Default model: {@code text-embedding-3-small} (1536 dims).</li>
 *     <li>Bounded, single-shot HTTPS call with a short timeout (&lt;= 5s)
 *     and a 1-retry budget.</li>
 *     <li>On failure / timeout, the caller is responsible for degrading per
 *     the design doc rules — hybrid silently falls back to text;
 *     vector returns 503.</li>
 * </ul>
 *
 * <p>The worker (Task 5) owns its own Python-side embedding helper; the two
 * live in different services and intentionally share no code in v1.
 */
public interface MemoryEmbeddingClient {

    /**
     * Computes an embedding for the given query string.
     *
     * @param query the query text (already validated non-blank by the caller)
     * @return the embedding result, with vector + token count + cost
     * @throws EmbeddingUnavailableException on any failure (provider down,
     *         timeout, credential lookup failure, invalid response)
     */
    EmbeddingResult embedQuery(String query);

    /** Outcome of a single embedding call. */
    record EmbeddingResult(
            float[] vector,
            int tokens,
            long costMicrodollars,
            String modelId) {
    }

    /** Raised on any failure in the embedding call. */
    class EmbeddingUnavailableException extends RuntimeException {
        public EmbeddingUnavailableException(String message) {
            super(message);
        }

        public EmbeddingUnavailableException(String message, Throwable cause) {
            super(message, cause);
        }
    }
}
