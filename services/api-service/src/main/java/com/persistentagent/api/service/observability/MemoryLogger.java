package com.persistentagent.api.service.observability;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Component;

/**
 * Structured log helpers for the memory REST surface.
 *
 * <p>Mirrors the key/value SLF4J style used by other services in this package
 * (e.g. {@link CheckpointObservabilityService}). No PII is emitted — the query
 * string itself is summarised by length, not contents.
 *
 * <p>Log event names:
 * <ul>
 *     <li>{@code memory.search.served} — one line per search call, tracking
 *     latency and result count.</li>
 *     <li>{@code memory.search.embedding} — emitted whenever the endpoint
 *     computed a query embedding. This is the substitute for the
 *     {@code agent_cost_ledger} row; the search-time embedding is logged,
 *     not ledgered, because the ledger requires {@code task_id} /
 *     {@code checkpoint_id} which do not exist on an API-driven search.</li>
 *     <li>{@code memory.delete.succeeded} — one line per successful delete.</li>
 * </ul>
 */
@Component
public class MemoryLogger {

    private static final Logger log = LoggerFactory.getLogger(MemoryLogger.class);

    /**
     * @param tenantId        tenant owning the agent
     * @param agentId         path agent id
     * @param modeRequested   {@code hybrid} / {@code text} / {@code vector}
     * @param rankingUsed     the path actually taken; equals {@code modeRequested}
     *                        unless a hybrid request degraded to {@code text}
     * @param latencyMs       wall-clock time spent serving the search
     * @param resultCount     number of rows returned to the caller
     * @param qLength         length of the input query string, in chars; the
     *                        query itself is NOT logged
     */
    public void searchServed(
            String tenantId,
            String agentId,
            String modeRequested,
            String rankingUsed,
            long latencyMs,
            int resultCount,
            int qLength) {
        log.info(
                "memory.search.served tenant_id={} agent_id={} mode_requested={} ranking_used={} latency_ms={} result_count={} q_length={}",
                tenantId, agentId, modeRequested, rankingUsed, latencyMs, resultCount, qLength);
    }

    /**
     * Emits one line per embedding call on the search path. The cost is
     * recorded here only — the platform-level {@code agent_cost_ledger} is not
     * written for search-time embeddings (design doc "Embeddings" section).
     */
    public void searchEmbedding(
            String tenantId,
            String agentId,
            int tokens,
            long costMicrodollars) {
        log.info(
                "memory.search.embedding tenant_id={} agent_id={} tokens={} cost_microdollars={}",
                tenantId, agentId, tokens, costMicrodollars);
    }

    /** Emitted when the embedding provider fails during a search. */
    public void searchEmbeddingFailed(
            String tenantId,
            String agentId,
            String errorClass,
            String errorMessage) {
        log.warn(
                "memory.search.embedding_failed tenant_id={} agent_id={} error_class={} error_message={}",
                tenantId, agentId, errorClass, errorMessage);
    }

    /** Emitted after a successful hard delete of a memory entry. */
    public void deleteSucceeded(String tenantId, String agentId, String memoryId) {
        log.info(
                "memory.delete.succeeded tenant_id={} agent_id={} memory_id={}",
                tenantId, agentId, memoryId);
    }
}
