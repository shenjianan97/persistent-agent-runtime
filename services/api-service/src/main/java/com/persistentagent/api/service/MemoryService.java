package com.persistentagent.api.service;

import com.persistentagent.api.config.ValidationConstants;
import com.persistentagent.api.exception.EmbeddingProviderUnavailableException;
import com.persistentagent.api.exception.MemoryNotFoundException;
import com.persistentagent.api.exception.ValidationException;
import com.persistentagent.api.model.response.MemoryEntryResponse;
import com.persistentagent.api.model.response.MemoryEntrySummary;
import com.persistentagent.api.model.response.MemoryListResponse;
import com.persistentagent.api.model.response.MemorySearchResponse;
import com.persistentagent.api.model.response.MemoryStorageStats;
import com.persistentagent.api.repository.MemoryRepository;
import com.persistentagent.api.service.observability.MemoryLogger;
import com.persistentagent.api.util.DateTimeUtil;
import org.springframework.stereotype.Service;

import java.nio.charset.StandardCharsets;
import java.time.OffsetDateTime;
import java.util.ArrayList;
import java.util.Base64;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.UUID;

/**
 * Orchestrates the memory REST surface: list, search (hybrid RRF / text /
 * vector), detail, delete. Enforces:
 *
 * <ul>
 *     <li>404-not-403 disclosure — any scope miss raises
 *     {@link MemoryNotFoundException} with a uniform message.</li>
 *     <li>RRF constants are platform-fixed in v1: {@code k=60},
 *     {@code candidate_multiplier=4}.</li>
 *     <li>Hybrid silently degrades to text when the embedding provider is
 *     unavailable; vector raises 503 instead.</li>
 *     <li>Search-time embeddings are logged, never written to
 *     {@code agent_cost_ledger}.</li>
 * </ul>
 */
@Service
public class MemoryService {

    /** Reciprocal-Rank-Fusion constant (design doc "Search API"). */
    static final int RRF_K = 60;

    /** Candidate pool multiplier applied to the requested {@code limit}. */
    static final int CANDIDATE_MULTIPLIER = 4;

    /** Default pagination limit for list. */
    static final int DEFAULT_LIST_LIMIT = 50;

    /** Max pagination limit for list. */
    static final int MAX_LIST_LIMIT = 200;

    /** Default search limit. */
    static final int DEFAULT_SEARCH_LIMIT = 5;

    /** Max search limit (REST; the worker tool layer caps at 10 — Task 7). */
    static final int MAX_SEARCH_LIMIT = 20;

    /** Characters taken from the full summary for {@code summary_preview}. */
    static final int SUMMARY_PREVIEW_CHARS = 200;

    static final String MODE_HYBRID = "hybrid";
    static final String MODE_TEXT = "text";
    static final String MODE_VECTOR = "vector";
    static final Set<String> VALID_MODES = Set.of(MODE_HYBRID, MODE_TEXT, MODE_VECTOR);

    static final Set<String> VALID_OUTCOMES = Set.of("succeeded", "failed");

    private final MemoryRepository repository;
    private final MemoryEmbeddingClient embeddingClient;
    private final MemoryLogger logger;

    public MemoryService(
            MemoryRepository repository,
            MemoryEmbeddingClient embeddingClient,
            MemoryLogger logger) {
        this.repository = repository;
        this.embeddingClient = embeddingClient;
        this.logger = logger;
    }

    // ----- list -----

    public MemoryListResponse list(
            String agentId,
            String outcome,
            OffsetDateTime from,
            OffsetDateTime to,
            Integer limit,
            String cursor) {
        String tenantId = ValidationConstants.DEFAULT_TENANT_ID;
        requireAgentInScope(tenantId, agentId);

        String normalizedOutcome = validateOutcomeFilter(outcome);
        int effectiveLimit = clampListLimit(limit);
        ListCursor parsedCursor = decodeCursor(cursor);

        // Fetch one extra row to detect whether a next page exists.
        List<Map<String, Object>> rows = repository.list(
                tenantId,
                agentId,
                normalizedOutcome,
                from,
                to,
                parsedCursor == null ? null : parsedCursor.createdAt(),
                parsedCursor == null ? null : parsedCursor.memoryId(),
                effectiveLimit + 1);

        boolean hasMore = rows.size() > effectiveLimit;
        List<Map<String, Object>> pageRows = hasMore ? rows.subList(0, effectiveLimit) : rows;

        List<MemoryEntrySummary> items = new ArrayList<>(pageRows.size());
        for (Map<String, Object> row : pageRows) {
            items.add(rowToSummary(row, /* includePreview */ false, /* score */ null));
        }

        String nextCursor = null;
        if (hasMore && !pageRows.isEmpty()) {
            Map<String, Object> last = pageRows.get(pageRows.size() - 1);
            nextCursor = encodeCursor(
                    DateTimeUtil.toOffsetDateTime(last.get("created_at")),
                    last.get("memory_id").toString());
        }

        MemoryStorageStats stats = null;
        if (parsedCursor == null) {
            long count = repository.countForAgent(tenantId, agentId);
            long bytes = repository.approxBytesForAgent(tenantId, agentId);
            stats = new MemoryStorageStats(count, bytes);
        }

        return new MemoryListResponse(items, nextCursor, stats);
    }

    // ----- detail -----

    public MemoryEntryResponse get(String agentId, String memoryId) {
        String tenantId = ValidationConstants.DEFAULT_TENANT_ID;
        requireAgentInScope(tenantId, agentId);
        validateUuid(memoryId);

        Map<String, Object> row = repository.findById(tenantId, agentId, memoryId)
                .orElseThrow(MemoryNotFoundException::new);
        return rowToEntry(row);
    }

    // ----- delete -----

    public void delete(String agentId, String memoryId) {
        String tenantId = ValidationConstants.DEFAULT_TENANT_ID;
        requireAgentInScope(tenantId, agentId);
        validateUuid(memoryId);

        boolean removed = repository.delete(tenantId, agentId, memoryId);
        if (!removed) {
            throw new MemoryNotFoundException();
        }
        logger.deleteSucceeded(tenantId, agentId, memoryId);
    }

    // ----- search -----

    public MemorySearchResponse search(
            String agentId,
            String query,
            String mode,
            Integer limit,
            String outcome,
            OffsetDateTime from,
            OffsetDateTime to) {
        String tenantId = ValidationConstants.DEFAULT_TENANT_ID;
        requireAgentInScope(tenantId, agentId);

        if (query == null || query.isBlank()) {
            throw new ValidationException("q is required");
        }
        String modeRequested = validateMode(mode);
        String normalizedOutcome = validateOutcomeFilter(outcome);
        int effectiveLimit = clampSearchLimit(limit);
        int candidateLimit = effectiveLimit * CANDIDATE_MULTIPLIER;

        long started = System.nanoTime();
        String rankingUsed;
        List<Map<String, Object>> rows;

        switch (modeRequested) {
            case MODE_TEXT -> {
                rows = repository.searchText(tenantId, agentId, query, normalizedOutcome, from, to, effectiveLimit);
                rankingUsed = MODE_TEXT;
            }
            case MODE_VECTOR -> {
                float[] vec = embedOrThrow(tenantId, agentId, query, /* allowDegrade */ false);
                rows = repository.searchVector(tenantId, agentId, vec, normalizedOutcome, from, to, effectiveLimit);
                rankingUsed = MODE_VECTOR;
            }
            case MODE_HYBRID -> {
                float[] vec = embedOrDegrade(tenantId, agentId, query);
                if (vec == null) {
                    // Silent degrade: provider failure on hybrid → BM25 only with ranking_used=text.
                    rows = repository.searchText(tenantId, agentId, query, normalizedOutcome, from, to, effectiveLimit);
                    rankingUsed = MODE_TEXT;
                } else {
                    rows = repository.searchHybrid(
                            tenantId, agentId, query, vec, normalizedOutcome, from, to,
                            candidateLimit, effectiveLimit, RRF_K);
                    rankingUsed = MODE_HYBRID;
                }
            }
            default -> throw new ValidationException("Unsupported mode: " + modeRequested);
        }

        List<MemoryEntrySummary> results = new ArrayList<>(rows.size());
        for (Map<String, Object> row : rows) {
            Double score = extractScore(row);
            results.add(rowToSummary(row, /* includePreview */ true, score));
        }

        long latencyMs = (System.nanoTime() - started) / 1_000_000;
        logger.searchServed(
                tenantId, agentId, modeRequested, rankingUsed,
                latencyMs, results.size(), query.length());
        return new MemorySearchResponse(results, rankingUsed);
    }

    // ----- internal: embedding call + observability -----

    /** For {@code mode=vector}: provider failure raises 503. */
    private float[] embedOrThrow(String tenantId, String agentId, String query, boolean allowDegrade) {
        try {
            MemoryEmbeddingClient.EmbeddingResult result = embeddingClient.embedQuery(query);
            logger.searchEmbedding(
                    tenantId, agentId, result.tokens(), result.costMicrodollars());
            return result.vector();
        } catch (MemoryEmbeddingClient.EmbeddingUnavailableException e) {
            logger.searchEmbeddingFailed(
                    tenantId, agentId, e.getClass().getSimpleName(),
                    e.getMessage() == null ? "" : e.getMessage());
            if (allowDegrade) {
                return null;
            }
            throw new EmbeddingProviderUnavailableException(
                    "Embedding provider unavailable");
        }
    }

    /** For {@code mode=hybrid}: provider failure degrades silently to text. */
    private float[] embedOrDegrade(String tenantId, String agentId, String query) {
        return embedOrThrow(tenantId, agentId, query, /* allowDegrade */ true);
    }

    // ----- validation helpers -----

    private void requireAgentInScope(String tenantId, String agentId) {
        if (agentId == null || agentId.isBlank()) {
            throw new MemoryNotFoundException();
        }
        if (!repository.agentExists(tenantId, agentId)) {
            // 404-not-403: caller cannot tell if the agent exists in another tenant.
            throw new MemoryNotFoundException();
        }
    }

    static int clampListLimit(Integer requested) {
        if (requested == null) return DEFAULT_LIST_LIMIT;
        if (requested < 1) {
            throw new ValidationException("limit must be >= 1");
        }
        if (requested > MAX_LIST_LIMIT) {
            throw new ValidationException("limit must be <= " + MAX_LIST_LIMIT);
        }
        return requested;
    }

    static int clampSearchLimit(Integer requested) {
        if (requested == null) return DEFAULT_SEARCH_LIMIT;
        if (requested < 1) {
            throw new ValidationException("limit must be >= 1");
        }
        if (requested > MAX_SEARCH_LIMIT) {
            throw new ValidationException("limit must be <= " + MAX_SEARCH_LIMIT);
        }
        return requested;
    }

    static String validateMode(String mode) {
        if (mode == null || mode.isBlank()) return MODE_HYBRID;
        String normalized = mode.toLowerCase();
        if (!VALID_MODES.contains(normalized)) {
            throw new ValidationException(
                    "mode must be one of " + VALID_MODES + " (got: " + mode + ")");
        }
        return normalized;
    }

    static String validateOutcomeFilter(String outcome) {
        if (outcome == null || outcome.isBlank()) return null;
        if (!VALID_OUTCOMES.contains(outcome)) {
            throw new ValidationException(
                    "outcome must be one of " + VALID_OUTCOMES + " (got: " + outcome + ")");
        }
        return outcome;
    }

    static void validateUuid(String value) {
        if (value == null) {
            throw new MemoryNotFoundException();
        }
        try {
            UUID.fromString(value);
        } catch (IllegalArgumentException e) {
            // Malformed UUID in the path is, for the 404-not-403 rule, the
            // same shape as an unknown id. Returning 400 would leak that the
            // id syntax is validated — but we only 404 here because it's a
            // scope miss in the same sense. Other endpoints in the codebase
            // return 400 for bad UUIDs; for the memory surface we want a
            // uniform "not found" to match "unknown id / wrong tenant".
            throw new MemoryNotFoundException();
        }
    }

    // ----- row → response mapping -----

    private MemoryEntrySummary rowToSummary(Map<String, Object> row, boolean includePreview, Double score) {
        String summary = (String) row.get("summary");
        String preview = includePreview && summary != null
                ? truncate(summary, SUMMARY_PREVIEW_CHARS)
                : null;
        return new MemoryEntrySummary(
                row.get("memory_id").toString(),
                (String) row.get("title"),
                (String) row.get("outcome"),
                row.get("task_id") == null ? null : row.get("task_id").toString(),
                DateTimeUtil.toOffsetDateTime(row.get("created_at")),
                preview,
                score);
    }

    private MemoryEntryResponse rowToEntry(Map<String, Object> row) {
        return new MemoryEntryResponse(
                row.get("memory_id").toString(),
                (String) row.get("agent_id"),
                row.get("task_id") == null ? null : row.get("task_id").toString(),
                (String) row.get("title"),
                (String) row.get("summary"),
                MemoryRepository.coerceTextArray(row.get("observations")),
                // Issue #102 — NULL for rows written before migration 0023.
                // ``coerceTextArray`` returns an empty list on NULL so the
                // caller never has to null-check.
                MemoryRepository.coerceTextArray(row.get("commit_rationales")),
                (String) row.get("outcome"),
                MemoryRepository.coerceTextArray(row.get("tags")),
                (String) row.get("summarizer_model_id"),
                row.get("version") == null ? 1 : ((Number) row.get("version")).intValue(),
                DateTimeUtil.toOffsetDateTime(row.get("created_at")),
                DateTimeUtil.toOffsetDateTime(row.get("updated_at")));
    }

    static String truncate(String value, int maxChars) {
        if (value == null || value.length() <= maxChars) return value;
        return value.substring(0, maxChars);
    }

    static Double extractScore(Map<String, Object> row) {
        Object v = row.get("rrf_score");
        if (v instanceof Number n) return n.doubleValue();
        v = row.get("rank");
        if (v instanceof Number n) return n.doubleValue();
        v = row.get("distance");
        if (v instanceof Number n) {
            // Surface cosine similarity as the score (1 - distance). Caller
            // sees a larger-is-better number in all modes.
            return 1.0 - n.doubleValue();
        }
        return null;
    }

    // ----- cursor encoding -----

    record ListCursor(OffsetDateTime createdAt, String memoryId) {}

    static String encodeCursor(OffsetDateTime createdAt, String memoryId) {
        String raw = createdAt.toString() + "|" + memoryId;
        return Base64.getUrlEncoder().withoutPadding()
                .encodeToString(raw.getBytes(StandardCharsets.UTF_8));
    }

    static ListCursor decodeCursor(String cursor) {
        if (cursor == null || cursor.isBlank()) return null;
        try {
            byte[] bytes = Base64.getUrlDecoder().decode(cursor);
            String raw = new String(bytes, StandardCharsets.UTF_8);
            int idx = raw.indexOf('|');
            if (idx <= 0 || idx == raw.length() - 1) {
                throw new ValidationException("Invalid cursor");
            }
            OffsetDateTime createdAt = OffsetDateTime.parse(raw.substring(0, idx));
            String memoryId = raw.substring(idx + 1);
            UUID.fromString(memoryId); // validate
            return new ListCursor(createdAt, memoryId);
        } catch (ValidationException e) {
            throw e;
        } catch (Exception e) {
            throw new ValidationException("Invalid cursor");
        }
    }

    /**
     * Exposed so controller tests can pass a Map-based row without touching
     * private state; intentionally package-private.
     */
    static Map<String, Object> synthRow(
            String memoryId, String title, String outcome, String taskId,
            OffsetDateTime createdAt, String summary, Double score) {
        Map<String, Object> row = new LinkedHashMap<>();
        row.put("memory_id", memoryId);
        row.put("title", title);
        row.put("outcome", outcome);
        row.put("task_id", taskId);
        row.put("created_at", createdAt);
        row.put("summary", summary);
        if (score != null) row.put("rrf_score", score);
        return row;
    }
}
