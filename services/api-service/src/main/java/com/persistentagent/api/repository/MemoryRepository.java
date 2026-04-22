package com.persistentagent.api.repository;

import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Repository;

import java.sql.Array;
import java.time.OffsetDateTime;
import java.util.ArrayList;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Objects;
import java.util.Optional;

/**
 * Repository for Phase 2 Track 5 memory entries (list, search, detail, delete,
 * storage stats). Every query MUST include both {@code tenant_id} and
 * {@code agent_id} predicates — the memory-query invariant from the design doc
 * "Validation and Consistency Rules" section. The public methods enforce that
 * at the entry point by requiring both parameters; internal callers that need
 * to build queries dynamically should go through the helpers here.
 */
@Repository
public class MemoryRepository {

    /** Column list returned by detail + list queries — keep in sync with schema. */
    static final String DETAIL_COLUMNS =
            "memory_id, tenant_id, agent_id, task_id, title, summary, observations, "
            + "commit_rationales, "
            + "outcome, tags, summarizer_model_id, version, created_at, updated_at";

    static final String SUMMARY_COLUMNS =
            "memory_id, title, outcome, task_id, summary, created_at";

    private final JdbcTemplate jdbcTemplate;

    public MemoryRepository(JdbcTemplate jdbcTemplate) {
        this.jdbcTemplate = jdbcTemplate;
    }

    /** Verifies the agent exists and belongs to the tenant. */
    public boolean agentExists(String tenantId, String agentId) {
        requireScope(tenantId, agentId);
        List<Integer> rows = jdbcTemplate.query(
                "SELECT 1 FROM agents WHERE tenant_id = ? AND agent_id = ? LIMIT 1",
                (rs, rowNum) -> rs.getInt(1),
                tenantId, agentId);
        return !rows.isEmpty();
    }

    /**
     * Lists memory entries scoped to {@code (tenant_id, agent_id)}, ordered
     * {@code (created_at DESC, memory_id DESC)}. Supports optional
     * {@code outcome}, {@code from}, {@code to} filters and cursor-based
     * pagination.
     *
     * @param limit max rows to return (1-based; the caller adds +1 if it wants
     *              hasMore detection)
     */
    public List<Map<String, Object>> list(
            String tenantId,
            String agentId,
            String outcome,
            OffsetDateTime from,
            OffsetDateTime to,
            OffsetDateTime cursorCreatedAt,
            String cursorMemoryId,
            int limit) {
        requireScope(tenantId, agentId);

        StringBuilder sql = new StringBuilder(
                "SELECT " + SUMMARY_COLUMNS
                        + " FROM agent_memory_entries"
                        + " WHERE tenant_id = ? AND agent_id = ?");
        List<Object> args = new ArrayList<>();
        args.add(tenantId);
        args.add(agentId);

        if (outcome != null) {
            sql.append(" AND outcome = ?");
            args.add(outcome);
        }
        if (from != null) {
            sql.append(" AND created_at >= ?");
            args.add(from);
        }
        if (to != null) {
            sql.append(" AND created_at <= ?");
            args.add(to);
        }
        // Cursor predicate uses (created_at, memory_id) lexicographic ordering.
        if (cursorCreatedAt != null && cursorMemoryId != null) {
            sql.append(" AND (created_at, memory_id) < (?, ?::uuid)");
            args.add(cursorCreatedAt);
            args.add(cursorMemoryId);
        }
        sql.append(" ORDER BY created_at DESC, memory_id DESC LIMIT ?");
        args.add(limit);

        return jdbcTemplate.queryForList(sql.toString(), args.toArray());
    }

    /** Returns one memory entry or empty. */
    public Optional<Map<String, Object>> findById(String tenantId, String agentId, String memoryId) {
        requireScope(tenantId, agentId);
        Objects.requireNonNull(memoryId, "memoryId");
        List<Map<String, Object>> rows = jdbcTemplate.queryForList(
                "SELECT " + DETAIL_COLUMNS
                        + " FROM agent_memory_entries"
                        + " WHERE tenant_id = ? AND agent_id = ? AND memory_id = ?::uuid",
                tenantId, agentId, memoryId);
        return rows.isEmpty() ? Optional.empty() : Optional.of(rows.get(0));
    }

    /** Deletes by id scoped to {@code (tenant_id, agent_id)}. Returns true if a row was removed. */
    public boolean delete(String tenantId, String agentId, String memoryId) {
        requireScope(tenantId, agentId);
        Objects.requireNonNull(memoryId, "memoryId");
        int rows = jdbcTemplate.update(
                "DELETE FROM agent_memory_entries"
                        + " WHERE tenant_id = ? AND agent_id = ? AND memory_id = ?::uuid",
                tenantId, agentId, memoryId);
        return rows > 0;
    }

    /** Returns the exact count of entries for the agent's scope. */
    public long countForAgent(String tenantId, String agentId) {
        requireScope(tenantId, agentId);
        Long count = jdbcTemplate.queryForObject(
                "SELECT COUNT(*) FROM agent_memory_entries"
                        + " WHERE tenant_id = ? AND agent_id = ?",
                Long.class, tenantId, agentId);
        return count == null ? 0L : count;
    }

    /**
     * Returns an order-of-magnitude approximation of storage used by this
     * agent's rows. Uses {@code SUM(pg_column_size(...))} over user-facing
     * columns plus a constant for the 1536-dim float4 vector — cheaper than
     * {@code pg_total_relation_size} on the whole table when a single agent
     * owns a fraction of rows (design doc "Scale and Operational Plan").
     */
    public long approxBytesForAgent(String tenantId, String agentId) {
        requireScope(tenantId, agentId);
        Long bytes = jdbcTemplate.queryForObject(
                "SELECT COALESCE(SUM("
                        + "  pg_column_size(title)"
                        + " + pg_column_size(summary)"
                        + " + pg_column_size(observations)"
                        + " + pg_column_size(tags)"
                        + " + 1536 * 4"
                        + "), 0) FROM agent_memory_entries"
                        + " WHERE tenant_id = ? AND agent_id = ?",
                Long.class, tenantId, agentId);
        return bytes == null ? 0L : bytes;
    }

    /**
     * BM25-only ranking (text mode, and the bm25 half of hybrid). Uses
     * {@code websearch_to_tsquery} — raw {@code to_tsquery} over user input
     * is forbidden by the design doc. Rows that do not match the tsquery at
     * all are excluded.
     */
    public List<Map<String, Object>> searchText(
            String tenantId,
            String agentId,
            String query,
            String outcome,
            OffsetDateTime from,
            OffsetDateTime to,
            int limit) {
        requireScope(tenantId, agentId);
        StringBuilder sql = new StringBuilder(
                "WITH q AS (SELECT websearch_to_tsquery('english', ?) AS tsq)"
                        + " SELECT " + SUMMARY_COLUMNS
                        + ", ts_rank_cd(content_tsv, q.tsq) AS rank"
                        + " FROM agent_memory_entries, q"
                        + " WHERE tenant_id = ? AND agent_id = ?"
                        + "   AND content_tsv @@ q.tsq");
        List<Object> args = new ArrayList<>();
        args.add(query);
        args.add(tenantId);
        args.add(agentId);
        appendOutcomeAndDateFilters(sql, args, outcome, from, to);
        sql.append(" ORDER BY rank DESC, created_at DESC LIMIT ?");
        args.add(limit);
        return jdbcTemplate.queryForList(sql.toString(), args.toArray());
    }

    /**
     * Vector-only ranking (vector mode, and the vector half of hybrid). Uses
     * cosine distance {@code <=>}. Rows with {@code content_vec IS NULL}
     * (deferred-embedding) are excluded — they remain findable via the text
     * branch.
     */
    public List<Map<String, Object>> searchVector(
            String tenantId,
            String agentId,
            float[] queryVec,
            String outcome,
            OffsetDateTime from,
            OffsetDateTime to,
            int limit) {
        requireScope(tenantId, agentId);
        String vec = toVectorLiteral(queryVec);
        StringBuilder sql = new StringBuilder(
                "SELECT " + SUMMARY_COLUMNS
                        + ", (content_vec <=> ?::vector) AS distance"
                        + " FROM agent_memory_entries"
                        + " WHERE tenant_id = ? AND agent_id = ?"
                        + "   AND content_vec IS NOT NULL");
        List<Object> args = new ArrayList<>();
        args.add(vec);
        args.add(tenantId);
        args.add(agentId);
        appendOutcomeAndDateFilters(sql, args, outcome, from, to);
        sql.append(" ORDER BY distance ASC, created_at DESC LIMIT ?");
        args.add(limit);
        return jdbcTemplate.queryForList(sql.toString(), args.toArray());
    }

    /**
     * Hybrid RRF ranking fused in SQL with {@code k=60} and a candidate pool
     * of {@code candidateLimit = 4 * limit} per design doc.
     *
     * <p>Each candidate from BM25 and cosine gets {@code 1 / (k + rank)} and
     * the two halves are summed. Tiebreak is {@code created_at DESC}.
     */
    public List<Map<String, Object>> searchHybrid(
            String tenantId,
            String agentId,
            String query,
            float[] queryVec,
            String outcome,
            OffsetDateTime from,
            OffsetDateTime to,
            int candidateLimit,
            int limit,
            int rrfK) {
        requireScope(tenantId, agentId);
        String vec = toVectorLiteral(queryVec);

        StringBuilder sql = new StringBuilder();
        sql.append("WITH q AS (SELECT websearch_to_tsquery('english', ?) AS tsq),");
        sql.append(" scoped AS (");
        sql.append("   SELECT ").append(SUMMARY_COLUMNS).append(", content_tsv, content_vec");
        sql.append("   FROM agent_memory_entries");
        sql.append("   WHERE tenant_id = ? AND agent_id = ?");
        // Date / outcome filters apply to the scoped set so both rankers see the same candidate pool.
        List<Object> filterArgs = new ArrayList<>();
        StringBuilder filters = new StringBuilder();
        appendOutcomeAndDateFilters(filters, filterArgs, outcome, from, to);
        sql.append(filters);
        sql.append(" ),");
        sql.append(" bm25 AS (");
        sql.append("   SELECT memory_id, row_number() OVER (ORDER BY ts_rank_cd(content_tsv, q.tsq) DESC, created_at DESC) AS r");
        sql.append("   FROM scoped, q");
        sql.append("   WHERE content_tsv @@ q.tsq");
        sql.append("   ORDER BY ts_rank_cd(content_tsv, q.tsq) DESC, created_at DESC");
        sql.append("   LIMIT ?");
        sql.append(" ),");
        sql.append(" vec AS (");
        sql.append("   SELECT memory_id, row_number() OVER (ORDER BY content_vec <=> ?::vector) AS r");
        sql.append("   FROM scoped");
        sql.append("   WHERE content_vec IS NOT NULL");
        sql.append("   ORDER BY content_vec <=> ?::vector");
        sql.append("   LIMIT ?");
        sql.append(" )");
        sql.append(" SELECT scoped.memory_id, scoped.title, scoped.outcome, scoped.task_id, scoped.summary, scoped.created_at,");
        sql.append("   (COALESCE(1.0 / (? + bm25.r), 0) + COALESCE(1.0 / (? + vec.r), 0)) AS rrf_score");
        sql.append(" FROM scoped");
        sql.append(" LEFT JOIN bm25 USING (memory_id)");
        sql.append(" LEFT JOIN vec USING (memory_id)");
        sql.append(" WHERE bm25.r IS NOT NULL OR vec.r IS NOT NULL");
        sql.append(" ORDER BY rrf_score DESC, scoped.created_at DESC");
        sql.append(" LIMIT ?");

        List<Object> args = new ArrayList<>();
        args.add(query);        // q CTE
        args.add(tenantId);
        args.add(agentId);
        args.addAll(filterArgs);
        args.add(candidateLimit); // bm25 LIMIT
        args.add(vec);            // vec <=> (ORDER BY)
        args.add(vec);            // vec <=> (ORDER BY duplicated once more is fine via binding)
        args.add(candidateLimit); // vec LIMIT
        args.add(rrfK);           // bm25 weight
        args.add(rrfK);           // vec weight
        args.add(limit);

        return jdbcTemplate.queryForList(sql.toString(), args.toArray());
    }

    // --- helpers ---

    private static void requireScope(String tenantId, String agentId) {
        if (tenantId == null || tenantId.isBlank()) {
            throw new IllegalArgumentException("tenant_id is required for every memory query");
        }
        if (agentId == null || agentId.isBlank()) {
            throw new IllegalArgumentException("agent_id is required for every memory query");
        }
    }

    private static void appendOutcomeAndDateFilters(
            StringBuilder sql,
            List<Object> args,
            String outcome,
            OffsetDateTime from,
            OffsetDateTime to) {
        if (outcome != null) {
            sql.append(" AND outcome = ?");
            args.add(outcome);
        }
        if (from != null) {
            sql.append(" AND created_at >= ?");
            args.add(from);
        }
        if (to != null) {
            sql.append(" AND created_at <= ?");
            args.add(to);
        }
    }

    /**
     * Renders a Java {@code float[]} as pgvector's text literal form
     * (e.g. {@code [0.1,0.2,0.3]}). We bind it as a string and cast to
     * {@code ::vector} at SQL level to avoid a JDBC type-handler dependency.
     */
    static String toVectorLiteral(float[] vec) {
        Objects.requireNonNull(vec, "vec");
        StringBuilder sb = new StringBuilder(vec.length * 10 + 2);
        sb.append('[');
        for (int i = 0; i < vec.length; i++) {
            if (i > 0) sb.append(',');
            // String.format locale-specific; force dot decimal separator with Locale.ROOT.
            sb.append(String.format(Locale.ROOT, "%.7f", vec[i]));
        }
        sb.append(']');
        return sb.toString();
    }

    /** Converts a JDBC array / list value for observations / tags into a Java {@code List<String>}. */
    public static List<String> coerceTextArray(Object value) {
        if (value == null) {
            return List.of();
        }
        if (value instanceof List<?> list) {
            List<String> out = new ArrayList<>(list.size());
            for (Object el : list) {
                if (el != null) out.add(el.toString());
            }
            return out;
        }
        if (value instanceof Array array) {
            try {
                Object raw = array.getArray();
                if (raw instanceof Object[] arr) {
                    List<String> out = new ArrayList<>(arr.length);
                    for (Object el : arr) {
                        if (el != null) out.add(el.toString());
                    }
                    return out;
                }
            } catch (Exception ignored) {
                // fall through
            }
        }
        if (value instanceof String[] arr) {
            return List.of(arr);
        }
        return List.of();
    }
}
